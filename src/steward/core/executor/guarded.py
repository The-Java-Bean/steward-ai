# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`ReadOnlyView` and `Guarded`: the executor's file handles (docs/adr/0005).

Path rules, checked on every call:

- Paths are `library:relative/path` inside a configured library. Segments may not be
  empty, `.` or `..`, contain NUL or `\\`, or be longer than 255 bytes. New names are
  NFC-normalized; existing paths are used exactly as on disk.
- Every folder on the way is opened relative to its parent with `O_NOFOLLOW`, so a
  symlink anywhere in the path refuses the call instead of leading elsewhere; the last
  component is never a symlink either.
- Writes go only into folders whose effective settings are writable (not
  `app-managed`, not `ignored`, not inside one). The library root itself can't be
  moved or removed.
- Nothing is overwritten: new files are written to a temp file, `fsync`ed and linked
  into place; moves use `renameat2(RENAME_NOREPLACE)`. Replacing a file's content is a
  separate, explicit operation (edits).

Folder setting changes (locks, types) are collected and applied by the executor in the
same transaction as the journal entry.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import secrets
import stat
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime

from blake3 import blake3
from sqlalchemy import Connection, select

from steward.core.db import Database, utcnow
from steward.core.db.schema import files
from steward.core.discovery.hashing import PREFIX
from steward.core.folders import store as folder_store
from steward.core.folders.paths import MAX_SEGMENT_BYTES, FolderRef, PathError, normalize_relpath
from steward.core.folders.policy import Library, Policy
from steward.sdk import ActionRefused, PathPolicy, PathState

TEMP_PREFIX = ".steward-tmp-"
"""Temporary files the executor writes before linking them into place (discovery
ignores them)."""
CHUNK = 1 << 20
LOCKED_TRASH = "locked"
"""Subfolder of a trash folder for things deleted from locked folders; it is locked
itself, so trashed private files stay hidden (plan §21, open question 2)."""

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_RENAME_NOREPLACE = 1
_libc = ctypes.CDLL(None, use_errno=True)
_renameat2 = getattr(_libc, "renameat2", None)

type DbOp = Callable[[Connection], None]


@dataclass(frozen=True, slots=True)
class Target:
    library: Library
    relpath: str

    @property
    def path(self) -> str:
        return f"{self.library.library_id}:{self.relpath}"

    @property
    def parts(self) -> list[str]:
        return self.relpath.split("/") if self.relpath else []

    @property
    def parent(self) -> str:
        return self.relpath.rsplit("/", 1)[0] if "/" in self.relpath else ""

    @property
    def name(self) -> str:
        return self.relpath.rsplit("/", 1)[-1]


def split_path(path: str) -> tuple[str, str]:
    library_id, sep, relpath = path.partition(":")
    if not sep or not library_id:
        raise ActionRefused(f"{path!r} must look like 'library:relative/path'")
    return library_id, relpath


def check_segments(relpath: str) -> None:
    if relpath == "":
        return
    if relpath.startswith("/") or relpath.endswith("/"):
        raise ActionRefused(f"{relpath!r}: no leading or trailing '/'")
    for segment in relpath.split("/"):
        if segment in ("", ".", ".."):
            raise ActionRefused(f"{relpath!r} has an empty, '.' or '..' segment")
        if "\x00" in segment or "\\" in segment:
            raise ActionRefused(f"{relpath!r} contains NUL or '\\'")
        if any(unicodedata.category(c) == "Cc" for c in segment):
            raise ActionRefused(f"{relpath!r} contains control characters")
        try:
            encoded = segment.encode("utf-8")
        except UnicodeEncodeError:
            raise ActionRefused(f"{relpath!r} isn't valid text") from None
        if len(encoded) > MAX_SEGMENT_BYTES:
            raise ActionRefused(f"a name in {relpath[:60]!r}… is longer than 255 bytes")


@dataclass
class ReadOnlyView:
    """What action types see while planning (the SDK's `ReadOnlyFS`)."""

    policy: Policy
    state_db: Database
    clock: Callable[[], datetime] = utcnow
    _now: datetime | None = field(default=None, init=False)

    # --- paths ----------------------------------------------------------------------

    def target(self, path: str, *, new: bool = False) -> Target:
        """Parse and check a path. `new`: a name being created (NFC-normalized)."""
        library_id, relpath = split_path(path)
        library = self.policy.libraries.get(library_id)
        if library is None:
            raise ActionRefused(f"unknown library {library_id!r}")
        if new:
            try:
                relpath = normalize_relpath(relpath)
            except PathError as exc:
                raise ActionRefused(str(exc)) from None
        check_segments(relpath)
        return Target(library, relpath)

    def now(self) -> datetime:
        if self._now is None:
            self._now = self.clock()
        return self._now

    def policy_of(self, target: Target, *, folder: bool) -> PathPolicy:
        relpath = target.relpath if folder else target.parent
        try:
            settings = self.policy.settings_for(FolderRef(target.library.library_id, relpath))
        except PathError as exc:
            raise ActionRefused(str(exc)) from None
        return PathPolicy(
            settings.type.name, settings.writable, settings.locked, settings.sensitivity
        )

    def settings(self, path: str) -> PathPolicy:
        target = self.target(path)
        is_dir = self.state(path).kind == "dir"
        return self.policy_of(target, folder=is_dir)

    # --- reading --------------------------------------------------------------------

    def state(self, path: str, *, content_hash: bool = False) -> PathState:
        target = self.target(path)
        if target.relpath == "":
            return PathState(target.path, "dir")
        try:
            with open_dir(target.library.root, target.parts[:-1]) as fd:
                try:
                    st = os.stat(target.name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    return PathState(target.path, "absent")
                if stat.S_ISLNK(st.st_mode):
                    raise ActionRefused(f"{target.path} is a symlink")
                if stat.S_ISDIR(st.st_mode):
                    return PathState(target.path, "dir")
                if not stat.S_ISREG(st.st_mode):
                    raise ActionRefused(f"{target.path} isn't a regular file or folder")
                digest = _hash_at(fd, target.name) if content_hash else None
                return PathState(target.path, "file", st.st_size, st.st_mtime_ns, digest)
        except _Missing:
            return PathState(target.path, "absent")

    def file_id(self, path: str) -> str | None:
        target = self.target(path)
        with self.state_db.read() as conn:
            found = conn.execute(
                select(files.c.file_id).where(
                    files.c.library_id == target.library.library_id,
                    files.c.relpath == target.relpath,
                )
            ).scalar_one_or_none()
        return str(found) if found is not None else None

    # --- role folders ---------------------------------------------------------------

    def trash_folder(self, path: str) -> str:
        target = self.target(path)
        role = self.policy.roles.get("trash")
        if role is not None and role.library_id == target.library.library_id:
            base = role.relpath
        else:
            base = folder_store.DEFAULT_ROLE_PATHS["trash"][0]
        if self.policy_of(target, folder=False).locked:
            base = f"{base}/{LOCKED_TRASH}"
        return f"{target.library.library_id}:{base}"

    def backup_folder(self) -> str:
        role = self.policy.roles.get("edit_backups")
        if role is None:
            first = min(self.policy.libraries.values(), key=lambda lib: lib.position, default=None)
            if first is None:
                raise ActionRefused("no library to keep edit backups in")
            return f"{first.library_id}:{folder_store.DEFAULT_ROLE_PATHS['edit_backups'][0]}"
        return str(role)

    def free_name(self, path: str) -> str:
        if self.state(path).kind == "absent":
            return path
        folder, _, name = path.rpartition("/") if "/" in path else path.partition(":")
        sep = "/" if "/" in path else ":"
        stem, dot, ext = name.rpartition(".")
        if not stem:  # no extension, or a dotfile
            stem, dot, ext = name, "", ""
        for n in range(2, 10_000):
            candidate = f"{folder}{sep}{stem} ({n}){dot}{ext}"
            if self.state(candidate).kind == "absent":
                return candidate
        raise ActionRefused(f"no free name next to {path}")


class Guarded(ReadOnlyView):
    """The write handle for one `apply` (the SDK's `GuardedFS`)."""

    def __init__(self, policy: Policy, state_db: Database, clock: Callable[[], datetime]) -> None:
        super().__init__(policy, state_db, clock)
        self.db_ops: list[DbOp] = []
        self.allowed_writes: set[str] | None = None
        """When set (by the executor, from the plan), writes outside these paths and their
        subfolders are refused."""

    # --- checks ---------------------------------------------------------------------

    def writable(self, target: Target, *, folder: bool = False) -> None:
        if target.relpath == "" and folder:
            raise ActionRefused("a library root can't be changed")
        if not self.policy_of(target, folder=False).writable:
            raise ActionRefused(f"{target.path}: the folder isn't writable")
        if folder and not self.policy_of(target, folder=True).writable:
            raise ActionRefused(f"{target.path}: the folder isn't writable")
        here = unicodedata.normalize("NFC", target.path)
        if self.allowed_writes is not None and not any(
            within(here, unicodedata.normalize("NFC", allowed)) for allowed in self.allowed_writes
        ):
            raise ActionRefused(f"{target.path} isn't among the paths the plan writes")

    # --- writes ---------------------------------------------------------------------

    def mkdir(self, path: str, *, parents: bool = False, mode: int = 0o755) -> list[str]:
        target = self.target(path, new=True)
        self.writable(target, folder=True)
        created: list[str] = []
        parts = target.parts
        with open_dir(
            target.library.root, parts[:-1], create=parents, mode=mode, created=created
        ) as fd:
            try:
                os.mkdir(target.name, mode, dir_fd=fd)
            except FileExistsError:
                raise ActionRefused(f"{target.path} already exists") from None
            _fsync_dir(fd)
        created.append(target.relpath)
        return [f"{target.library.library_id}:{p}" for p in created]

    def rmdir(self, path: str) -> None:
        target = self.target(path)
        self.writable(target, folder=True)
        with open_dir(target.library.root, target.parts[:-1]) as fd:
            st = _lstat(fd, target.name, target.path)
            if not stat.S_ISDIR(st.st_mode):
                raise ActionRefused(f"{target.path} isn't a folder")
            try:
                os.rmdir(target.name, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    raise ActionRefused(f"{target.path} isn't empty") from None
                raise
            _fsync_dir(fd)

    def move(self, source: str, destination: str, *, parents: bool = False) -> list[str]:
        src = self.target(source)
        dst = self.target(destination, new=True)
        if src.relpath == "":
            raise ActionRefused("a library root can't be moved")
        with open_dir(src.library.root, src.parts[:-1]) as src_fd:
            st = _lstat(src_fd, src.name, src.path)
            is_dir = stat.S_ISDIR(st.st_mode)
            if not (is_dir or stat.S_ISREG(st.st_mode)):
                raise ActionRefused(f"{src.path} isn't a regular file or folder")
            if is_dir and src.library.library_id != dst.library.library_id:
                raise ActionRefused("folders can only be moved within their library")
            if is_dir and within(dst.path, src.path):
                raise ActionRefused(f"can't move {src.path} into itself")
            self.writable(src, folder=is_dir)
            self.writable(dst, folder=is_dir)
            created: list[str] = []
            with open_dir(
                dst.library.root, dst.parts[:-1], create=parents, created=created
            ) as dst_fd:
                try:
                    _rename_noreplace(src_fd, src.name, dst_fd, dst.name)
                except FileExistsError:
                    raise ActionRefused(f"{dst.path} already exists") from None
                except OSError as exc:
                    if exc.errno == errno.EXDEV:
                        raise ActionRefused(
                            f"{src.path} and {dst.path} are on different disks or mounts"
                        ) from None
                    raise
                _fsync_dir(dst_fd)
            _fsync_dir(src_fd)
        return [f"{dst.library.library_id}:{c}" for c in created]

    def create(self, path: str, data: bytes, *, mode: int = 0o644) -> None:
        target = self.target(path, new=True)
        self.writable(target)
        with open_dir(target.library.root, target.parts[:-1]) as fd:
            _link_new(fd, target, lambda out: os.write(out, data) if data else 0, mode)

    def copy(self, source: str, destination: str, *, parents: bool = False) -> list[str]:
        src = self.target(source)
        dst = self.target(destination, new=True)
        self.writable(dst)
        with open_dir(src.library.root, src.parts[:-1]) as src_fd:
            fd_in = _open_file(src_fd, src)
            try:
                mode = stat.S_IMODE(os.fstat(fd_in).st_mode)
                created: list[str] = []
                with open_dir(
                    dst.library.root, dst.parts[:-1], create=parents, created=created
                ) as dst_fd:

                    def write(out: int) -> int:
                        total = 0
                        while chunk := os.read(fd_in, CHUNK):
                            total += _write_all(out, chunk)
                        return total

                    _link_new(dst_fd, dst, write, mode)
            finally:
                os.close(fd_in)
        return [f"{dst.library.library_id}:{c}" for c in created]

    def replace(self, path: str, data: bytes) -> None:
        target = self.target(path)
        self.writable(target)
        with open_dir(target.library.root, target.parts[:-1]) as fd:
            st = _lstat(fd, target.name, target.path)
            if not stat.S_ISREG(st.st_mode):
                raise ActionRefused(f"{target.path} isn't a regular file")
            temp = _temp_name()
            out = os.open(
                temp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                stat.S_IMODE(st.st_mode),
                dir_fd=fd,
            )
            try:
                try:
                    _write_all(out, data)
                    os.fsync(out)
                finally:
                    os.close(out)
                os.rename(temp, target.name, src_dir_fd=fd, dst_dir_fd=fd)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temp, dir_fd=fd)
                raise
            _fsync_dir(fd)

    def read(self, path: str, *, max_bytes: int) -> bytes:
        target = self.target(path)
        with open_dir(target.library.root, target.parts[:-1]) as fd:
            fd_in = _open_file(fd, target)
            try:
                data = b""
                while len(data) <= max_bytes and (chunk := os.read(fd_in, CHUNK)):
                    data += chunk
            finally:
                os.close(fd_in)
        if len(data) > max_bytes:
            raise ActionRefused(f"{target.path} is larger than {max_bytes} bytes")
        return data

    # --- folder settings (applied with the journal entry) ---------------------------

    def set_lock(self, path: str, locked: bool) -> None:
        target = self.target(path)
        ref = FolderRef(target.library.library_id, target.relpath)
        actor = "executor"

        def op(conn: Connection) -> None:
            if locked:
                folder_store.lock(conn, ref, actor=actor)
            else:
                try:
                    folder_store.unlock(conn, ref, actor=actor)
                except folder_store.FolderError as exc:
                    raise ActionRefused(str(exc)) from None

        self.db_ops.append(op)

    def set_folder_type(self, path: str, type_name: str) -> None:
        target = self.target(path)
        ref = FolderRef(target.library.library_id, target.relpath)
        self.db_ops.append(
            lambda conn: folder_store.set_type(conn, ref, type_name, actor="executor")
        )


# --- low-level helpers ----------------------------------------------------------------


class _Missing(Exception):
    """A folder on the way doesn't exist."""


FolderMissing = _Missing
"""Public name for readers outside the executor (`core.files`)."""


@contextlib.contextmanager
def open_dir(
    root: str,
    parts: list[str],
    *,
    create: bool = False,
    mode: int = 0o755,
    created: list[str] | None = None,
) -> Iterator[int]:
    """Open the folder `root/parts...` without following symlinks (the root itself is
    configured, so it may be one). Raises `_Missing` if a folder doesn't exist (unless
    `create`), `ActionRefused` for symlinks and files in the way."""
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise ActionRefused(f"library root {root} can't be opened: {exc.strerror}") from None
    try:
        for i, part in enumerate(parts):
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise _Missing(part) from None
                os.mkdir(part, mode, dir_fd=fd)
                if created is not None:
                    created.append("/".join(parts[: i + 1]))
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ActionRefused(
                        f"{'/'.join(parts[: i + 1])!r} is a symlink or a file, not a folder"
                    ) from None
                raise
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _lstat(fd: int, name: str, path: str) -> os.stat_result:
    try:
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        raise ActionRefused(f"{path} doesn't exist") from None
    if stat.S_ISLNK(st.st_mode):
        raise ActionRefused(f"{path} is a symlink")
    return st


def _open_file(dir_fd: int, target: Target) -> int:
    try:
        fd = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        raise ActionRefused(f"{target.path} doesn't exist") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ActionRefused(f"{target.path} is a symlink") from None
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ActionRefused(f"{target.path} isn't a regular file")
    return fd


def _hash_at(dir_fd: int, name: str) -> str:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        hasher = blake3()
        while chunk := os.read(fd, CHUNK):
            hasher.update(chunk)
    finally:
        os.close(fd)
    return PREFIX["full"] + hasher.hexdigest()


def content_hash(data: bytes) -> str:
    return PREFIX["full"] + blake3(data).hexdigest()


def _temp_name() -> str:
    return TEMP_PREFIX + secrets.token_hex(8)


def _write_all(fd: int, data: bytes) -> int:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]
    return len(data)


def _link_new(dir_fd: int, target: Target, write: Callable[[int], int], mode: int) -> None:
    temp = _temp_name()
    out = os.open(
        temp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        mode,
        dir_fd=dir_fd,
    )
    try:
        try:
            write(out)
            os.fsync(out)
        finally:
            os.close(out)
        try:
            os.link(temp, target.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
        except FileExistsError:
            raise ActionRefused(f"{target.path} already exists") from None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temp, dir_fd=dir_fd)
    _fsync_dir(dir_fd)


def _rename_noreplace(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
    if _renameat2 is not None:
        if _renameat2(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), _RENAME_NOREPLACE) == 0:
            return
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise FileExistsError(err, os.strerror(err), dst)
        if err not in (errno.ENOSYS, errno.EINVAL):
            raise OSError(err, os.strerror(err), dst)
    # No RENAME_NOREPLACE here: check, then rename. Safe against other steward writers
    # (the executor holds the write lock), not against other programs.
    try:
        os.stat(dst, dir_fd=dst_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.rename(src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
        return
    raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dst)


def _fsync_dir(fd: int) -> None:
    with contextlib.suppress(OSError):
        os.fsync(fd)


def within(path: str, parent: str) -> bool:
    """`path` is `parent` or inside it (both `library:relpath`)."""
    library, _, relpath = path.partition(":")
    parent_library, _, parent_relpath = parent.partition(":")
    if library != parent_library:
        return False
    return (
        parent_relpath == ""
        or relpath == parent_relpath
        or relpath.startswith(parent_relpath + "/")
    )
