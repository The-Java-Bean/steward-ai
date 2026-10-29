# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Resumable uploads (plan §13.5): the bytes go into a hidden temporary file in the
destination folder, one whole chunk at a time, and finishing runs the journaled
`file.upload` action that renames it into place.

- `start` checks the folder (writable, visible to the caller), the name, collisions and
  free space, and creates `.steward-tmp-up-<id>` (exclusive, no symlinks).
- `write` takes a chunk at exactly the offset the server has (`received`), so a client
  that lost its connection asks for the offset and carries on. A chunk is written only
  after its whole body arrived, so `received` is always a chunk boundary; a temp file
  longer than `received` (a crash between writing and recording) is cut back.
- `finish` needs every byte; a name taken meanwhile answers `UploadConflict` with a free
  name, and the caller may finish under that name instead ("keep both").
- `cancel` and `expire` remove the temp file. Nothing here ever overwrites a file.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from steward.core.config import UploadsConfig
from steward.core.db import Databases, utcnow
from steward.core.db.schema import uploads
from steward.core.executor.executor import Executor, JournalEntry, SensitiveActionError
from steward.core.executor.guarded import (
    TEMP_PREFIX,
    UPLOAD_PREFIX,
    FolderMissing,
    ReadOnlyView,
    check_segments,
    open_dir,
)
from steward.core.folders import load_policy
from steward.sdk import ActionRefused

GIB = 1024**3
MIB = 1024**2
FREE_SPACE_MARGIN = 64 * MIB
"""Kept free on the destination disk beyond the upload itself."""
_FLAGS = os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC


class UploadNotFound(ActionRefused):
    """No such upload, or not the caller's, or not open any more."""


class UploadConflict(ActionRefused):
    """The name is taken. `suggestion` is a free name next to it."""

    def __init__(self, message: str, suggestion: str) -> None:
        super().__init__(message)
        self.suggestion = suggestion


class OffsetMismatch(ActionRefused):
    """The chunk doesn't start where the server's copy ends (`offset`)."""

    def __init__(self, offset: int) -> None:
        super().__init__(f"the upload continues at byte {offset}")
        self.offset = offset


@dataclass(frozen=True, slots=True)
class UploadInfo:
    upload_id: str
    library_id: str
    folder: str
    name: str
    size: int
    received: int
    status: str
    confirmed: bool
    entry_id: int | None
    created_at: datetime
    updated_at: datetime

    @property
    def folder_path(self) -> str:
        return f"{self.library_id}:{self.folder}"

    @property
    def path(self) -> str:
        return _join(self.folder_path, self.name)

    @property
    def temp(self) -> str:
        return _join(self.folder_path, UPLOAD_PREFIX + self.upload_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "upload_id": self.upload_id,
            "folder": self.folder_path,
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "offset": self.received,
            "status": self.status,
            "entry_id": self.entry_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class Uploads:
    def __init__(
        self,
        dbs: Databases,
        executor: Executor,
        config: UploadsConfig,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.dbs = dbs
        self.executor = executor
        self.config = config
        self.clock = clock
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    @property
    def chunk_bytes(self) -> int:
        return self.config.chunk_mib * MIB

    # --- starting --------------------------------------------------------------------

    def start(
        self, folder: str, name: str, size: int, *, actor: str, confirmed: bool = False
    ) -> UploadInfo:
        """Raises `ActionRefused` (incl. `UploadConflict`), or `SensitiveActionError` for a
        locked folder without `confirmed`."""
        name = _check_name(name)
        if size < 0:
            raise ActionRefused("the size can't be negative")
        if self.config.max_file_gib is not None and size > self.config.max_file_gib * GIB:
            raise ActionRefused(f"files over {self.config.max_file_gib:g} GiB aren't accepted")
        view = self._view()
        target = view.target(folder)
        if view.state(folder).kind != "dir":
            raise ActionRefused(f"{folder} isn't a folder")
        settings = view.policy_of(target, folder=True)
        if not settings.writable:
            raise ActionRefused(f"{folder}: the folder isn't writable")
        if settings.locked and not confirmed:
            raise SensitiveActionError("uploading into a locked folder needs your password")
        path = _join(target.path, name)
        if view.state(path).kind != "absent":
            raise UploadConflict(f"{name} already exists here", _name(view.free_name(path)))
        now = self.clock()
        with self.dbs.state.read() as conn:
            open_now = conn.execute(
                select(uploads.c.upload_id).where(uploads.c.status == "open")
            ).all()
        if len(open_now) >= self.config.max_open:
            raise ActionRefused("too many unfinished uploads; finish or cancel some first")
        upload_id = secrets.token_hex(16)
        mode = 0o600 if settings.locked else 0o644
        try:
            with open_dir(target.library.root, target.parts) as dir_fd:
                _check_space(dir_fd, size)
                fd = os.open(
                    UPLOAD_PREFIX + upload_id, _FLAGS | os.O_CREAT | os.O_EXCL, mode, dir_fd=dir_fd
                )
                os.close(fd)
                os.fsync(dir_fd)
        except FolderMissing:
            raise ActionRefused(f"{folder} doesn't exist") from None
        with self.dbs.state.write() as conn:
            conn.execute(
                uploads.insert().values(
                    upload_id=upload_id,
                    library_id=target.library.library_id,
                    folder=target.relpath,
                    name=name,
                    size=size,
                    received=0,
                    status="open",
                    confirmed=confirmed,
                    actor=actor,
                    entry_id=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        return self.get(upload_id, actor=actor)

    # --- looking ---------------------------------------------------------------------

    def get(self, upload_id: str, *, actor: str) -> UploadInfo:
        with self.dbs.state.read() as conn:
            row = conn.execute(
                select(uploads).where(uploads.c.upload_id == upload_id)
            ).one_or_none()
        if row is None or row.actor != actor:
            raise UploadNotFound("no such upload")
        return _info(row)

    def list_open(self, *, actor: str) -> list[UploadInfo]:
        with self.dbs.state.read() as conn:
            rows = conn.execute(
                select(uploads)
                .where(uploads.c.status == "open", uploads.c.actor == actor)
                .order_by(uploads.c.created_at)
            ).all()
        return [_info(r) for r in rows]

    # --- writing ---------------------------------------------------------------------

    def write(self, upload_id: str, offset: int, data: bytes, *, actor: str) -> UploadInfo:
        if len(data) > self.chunk_bytes:
            raise ActionRefused(f"chunks are at most {self.config.chunk_mib} MiB")
        with self._lock(upload_id):
            info = self._open(upload_id, actor)
            if offset != info.received:
                raise OffsetMismatch(info.received)
            if offset + len(data) > info.size:
                raise ActionRefused("more bytes than the upload's size")
            if data:
                self._append(info, data)
            with self.dbs.state.write() as conn:
                conn.execute(
                    update(uploads)
                    .where(uploads.c.upload_id == upload_id)
                    .values(received=offset + len(data), updated_at=self.clock())
                )
            return self.get(upload_id, actor=actor)

    def _append(self, info: UploadInfo, data: bytes) -> None:
        view = self._view()
        folder = view.target(info.folder_path)
        try:
            with open_dir(folder.library.root, folder.parts) as dir_fd:
                try:
                    fd = os.open(UPLOAD_PREFIX + info.upload_id, _FLAGS, dir_fd=dir_fd)
                except FileNotFoundError:
                    raise UploadNotFound("the upload's temporary file is gone") from None
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode):
                        raise UploadNotFound("the upload's temporary file is gone")
                    if st.st_size != info.received:
                        os.ftruncate(fd, info.received)
                    view_ = memoryview(data)
                    offset = info.received
                    while view_:
                        written = os.pwrite(fd, view_, offset)
                        offset += written
                        view_ = view_[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except FolderMissing:
            raise UploadNotFound("the destination folder is gone") from None
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise ActionRefused("the disk is full") from None
            raise

    # --- finishing -------------------------------------------------------------------

    def finish(
        self, upload_id: str, *, actor: str, name: str | None = None, confirmed: bool = False
    ) -> JournalEntry:
        """Put the file in place under its name (or `name`). Raises `UploadConflict` when
        the name is taken, `ActionRefused` if bytes are missing."""
        with self._lock(upload_id):
            info = self._open(upload_id, actor)
            if info.received != info.size:
                raise ActionRefused(f"{info.size - info.received} bytes are still missing")
            final = _check_name(name) if name is not None else info.name
            path = _join(info.folder_path, final)
            view = self._view()
            if view.state(path).kind != "absent":
                raise UploadConflict(f"{final} already exists here", _name(view.free_name(path)))
            params = {"temp": info.temp, "path": path, "size": info.size}
            try:
                entry = self.executor.run(
                    "file.upload", params, actor=actor, confirmed=confirmed or info.confirmed
                )
            except ActionRefused as exc:
                if "already exists" in str(exc):
                    raise UploadConflict(str(exc), _name(self._view().free_name(path))) from None
                raise
            with self.dbs.state.write() as conn:
                conn.execute(
                    update(uploads)
                    .where(uploads.c.upload_id == upload_id)
                    .values(
                        status="finished",
                        name=final,
                        entry_id=entry.entry_id,
                        updated_at=self.clock(),
                    )
                )
            return entry

    # --- ending ----------------------------------------------------------------------

    def cancel(self, upload_id: str, *, actor: str) -> None:
        with self._lock(upload_id):
            info = self._open(upload_id, actor)
            self._end(info, "cancelled")

    def expire(self) -> int:
        """Remove unfinished uploads untouched for `stale_hours`; returns how many."""
        cutoff = self.clock() - timedelta(hours=self.config.stale_hours)
        with self.dbs.state.read() as conn:
            rows = conn.execute(
                select(uploads).where(uploads.c.status == "open", uploads.c.updated_at < cutoff)
            ).all()
        count = 0
        for row in rows:
            with self._lock(row.upload_id):
                self._end(_info(row), "expired")
                count += 1
        return count

    def _end(self, info: UploadInfo, status: str) -> None:
        view = self._view()
        try:
            folder = view.target(info.folder_path)
            with open_dir(folder.library.root, folder.parts) as dir_fd:
                try:
                    st = os.stat(
                        UPLOAD_PREFIX + info.upload_id, dir_fd=dir_fd, follow_symlinks=False
                    )
                    if stat.S_ISREG(st.st_mode):
                        os.unlink(UPLOAD_PREFIX + info.upload_id, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
        except (FolderMissing, ActionRefused):
            pass  # the folder or library is gone, and the temp file with it
        with self.dbs.state.write() as conn:
            conn.execute(
                update(uploads)
                .where(uploads.c.upload_id == info.upload_id, uploads.c.status == "open")
                .values(status=status, updated_at=self.clock())
            )
        self._locks.pop(info.upload_id, None)

    # --- helpers ---------------------------------------------------------------------

    def _open(self, upload_id: str, actor: str) -> UploadInfo:
        info = self.get(upload_id, actor=actor)
        if info.status != "open":
            raise UploadNotFound(f"the upload is {info.status}")
        return info

    def _view(self) -> ReadOnlyView:
        with self.dbs.state.read() as conn:
            policy = load_policy(conn)
        return ReadOnlyView(policy, self.dbs.state, self.clock)

    def _lock(self, upload_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(upload_id, threading.Lock())


def _check_name(name: str) -> str:
    name = unicodedata.normalize("NFC", name.strip())
    if not name or "/" in name or name in (".", ".."):
        raise ActionRefused("a file name can't be empty, contain '/' or be '.' or '..'")
    if name.startswith(TEMP_PREFIX):
        raise ActionRefused("that name is reserved for steward-ai's temporary files")
    check_segments(name)
    return name


def _check_space(dir_fd: int, size: int) -> None:
    st = os.fstatvfs(dir_fd)
    if st.f_bavail * st.f_frsize < size + FREE_SPACE_MARGIN:
        raise ActionRefused("there isn't enough free space on that disk")


def _join(folder: str, name: str) -> str:
    return f"{folder}{name}" if folder.endswith(":") else f"{folder}/{name}"


def _name(path: str) -> str:
    relpath = path.partition(":")[2]
    return relpath.rsplit("/", 1)[-1]


def _info(row: Any) -> UploadInfo:
    return UploadInfo(
        upload_id=row.upload_id,
        library_id=row.library_id,
        folder=row.folder,
        name=row.name,
        size=row.size,
        received=row.received,
        status=row.status,
        confirmed=bool(row.confirmed),
        entry_id=row.entry_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
