# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Discovery: compare what's on disk with the `folders` and `files` tables.

A scan walks a library (or part of one), then:

1. **Folders**, shallowest first. A folder with no identity at its path that has the
   inode of a folder that's gone from its old path is a rename or move: its identity
   (and with it the mapping, lock and roles, and the files inside) follows it
   (`store.move_folder`). If the old folder had files, at least one of them must be
   in the new one too, so a reused inode can't hand a mapping to an unrelated folder.
2. **Files.** Same path: unchanged if size, mtime and inode match, otherwise re-hashed
   (an edit keeps its `file_id`). New path: a vanished file with the same inode, size
   and mtime is a rename (no re-hash); otherwise the file is hashed and a vanished
   file with the same content hash (for sampled hashes, also size and mtime) is a move,
   in any library. Anything else is a new file.
3. **Missing.** Rows inside the scanned area that weren't found are marked missing.
   They're never deleted. Folders that couldn't be read don't count as missing.

"Vanished" means: no longer at its recorded path, and either still marked present or
missing for less than `move_window_hours`. Discovery only reads the libraries; it
writes nothing but the databases. Paths are stored exactly as on disk (no Unicode
normalization), so they always lead back to the file.

Files that are `excluded` (secret patterns and rules in `privacy.yaml`, an `excluded`
folder type, or a per-file override) are never opened: only their path, size, mtime and
inode are recorded, they have no content hash, and so no stage ever runs on them.
"""

from __future__ import annotations

import logging
import os
import stat
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, func, literal, select, update
from sqlalchemy.dialects.sqlite import insert

from steward.core import events
from steward.core.config import DiscoverySettings
from steward.core.db import Databases, utcnow
from steward.core.db.schema import blobs, files, folders, path_history, stage_runs
from steward.core.db.types import UTCDateTime
from steward.core.discovery.filters import is_ignored
from steward.core.discovery.hashing import (
    HASH_STAGE,
    HASH_STAGE_VERSION,
    HashKind,
    HashResult,
    Throttle,
    hash_file,
)
from steward.core.folders import FolderError, FolderRef, Policy, is_within, load_policy
from steward.core.folders import store as folder_store
from steward.core.folders.policy import EffectiveSettings
from steward.core.privacy import PrivacyRules, load_overrides

log = logging.getLogger(__name__)

BATCH = 500
RENEW_EVERY_SECONDS = 60


class ScanError(Exception):
    """The library can't be scanned safely (root missing or unexpectedly empty)."""


@dataclass
class ScanReport:
    files_new: int = 0
    files_changed: int = 0
    files_moved: int = 0
    files_reappeared: int = 0
    files_missing: int = 0
    files_unchanged: int = 0
    folders_new: int = 0
    folders_moved: int = 0
    folders_missing: int = 0
    unstable: int = 0
    """Files that changed while being hashed; the next scan picks them up."""
    errors: int = 0
    """Files or folders that couldn't be read, or names that aren't valid UTF-8."""

    def as_dict(self) -> dict[str, int]:
        return dict(vars(self))


@dataclass(frozen=True, slots=True)
class _DiskFile:
    size: int
    mtime_ns: int
    inode: int
    hash_policy: HashKind
    excluded: bool = False
    """Content must never be read (privacy rules or an `excluded` folder)."""


@dataclass
class _Walk:
    dirs: dict[str, int] = field(default_factory=dict)
    """relpath -> inode, for every folder found (including the scanned one)."""
    files: dict[str, _DiskFile] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)

    def readable(self, relpath: str) -> bool:
        return not any(is_within(relpath, d) for d in self.unreadable)


class Scanner:
    def __init__(
        self,
        dbs: Databases,
        settings: DiscoverySettings,
        *,
        actor: str = "system",
        renew: Callable[[], None] | None = None,
        clock: Callable[[], datetime] = utcnow,
        allow_empty_root: bool = False,
        privacy: PrivacyRules | None = None,
    ) -> None:
        self.dbs = dbs
        self.privacy = privacy if privacy is not None else PrivacyRules.default()
        self.allow_empty_root = allow_empty_root
        """An empty library root usually means an unmounted disk, so by default a scan
        refuses to mark everything missing; set this when the library really is empty."""
        self.settings = settings
        self.actor = actor
        self.renew = renew
        self.clock = clock
        self.throttle = Throttle(settings.max_read_mib_per_second)
        self._renewed = time.monotonic()
        self._settings_cache: dict[str, EffectiveSettings] = {}
        self._overrides: Mapping[str, str] = {}
        self._last_error: str | None = None

    # --- entry point ------------------------------------------------------------

    def scan(self, library_id: str, paths: Sequence[str] = ("",)) -> ScanReport:
        """Scan `paths` (relative to the library root; "" is the whole library)."""
        with self.dbs.state.read() as conn:
            policy = load_policy(conn)
        library = policy.library(library_id)
        root = Path(library.root)
        if not root.is_dir():
            raise ScanError(f"library {library_id} root {root} isn't a directory (not mounted?)")
        self._settings_cache = {}
        with self.dbs.state.read() as conn:
            self._overrides = load_overrides(conn)
        report = ScanReport()
        # Paths that exist go first, so a move's destination claims the identity before
        # its source is marked missing.
        for relpath in sorted(_outermost(paths), key=lambda p: (not _exists(root, p), p)):
            self._scan_one(policy, library_id, root, relpath, report)
        return report

    def _scan_one(
        self, policy: Policy, library_id: str, root: Path, relpath: str, report: ScanReport
    ) -> None:
        walk = _Walk() if self._excluded(policy, library_id, relpath) else None
        if walk is None:
            walk = self._walk(policy, library_id, root, relpath, report)
        if relpath == "" and not self.allow_empty_root and _empty_dir(root):
            with self.dbs.state.read() as conn:
                known = conn.execute(
                    select(files.c.file_id)
                    .where(files.c.library_id == library_id, files.c.present.is_(True))
                    .limit(1)
                ).first()
            if known:
                raise ScanError(
                    f"library {library_id} root {root} is empty but files were indexed there; "
                    "not marking them missing (is the disk mounted? If the library really is "
                    "empty, run `steward scan --allow-empty`)"
                )
        self._folders(library_id, root, walk, report)
        self._files(policy, library_id, root, relpath, walk, report)
        self._missing(library_id, relpath, walk, report)

    # --- walking ------------------------------------------------------------------

    def _effective(self, policy: Policy, library_id: str, relpath: str) -> EffectiveSettings:
        cached = self._settings_cache.get(relpath)
        if cached is None:
            cached = policy.settings_for(FolderRef(library_id, relpath))
            self._settings_cache[relpath] = cached
        return cached

    def _patterns(self, policy: Policy, library_id: str, folder: str) -> tuple[str, ...]:
        return (
            *self.settings.ignore_patterns,
            *self._effective(policy, library_id, folder).ignore,
        )

    def _excluded(self, policy: Policy, library_id: str, relpath: str) -> bool:
        """True if `relpath` is inside a folder that isn't indexed or has an ignored name."""
        parts = relpath.split("/") if relpath else []
        if self._effective(policy, library_id, "").index == "none":
            return True
        for i in range(1, len(parts) + 1):
            path, parent = "/".join(parts[:i]), "/".join(parts[: i - 1])
            if is_ignored(path, self._patterns(policy, library_id, parent)):
                return True
            if i < len(parts) and self._effective(policy, library_id, path).index == "none":
                return True
        return False

    def _secret(self, settings: EffectiveSettings, relpath: str) -> bool:
        return settings.sensitivity == "excluded" or self.privacy.level_for(relpath) == "excluded"

    def _walk(
        self, policy: Policy, library_id: str, root: Path, relpath: str, report: ScanReport
    ) -> _Walk:
        walk = _Walk()
        try:
            st = (root / relpath).lstat() if relpath else root.stat()
        except FileNotFoundError:
            return walk
        except OSError:
            walk.unreadable.append(relpath)
            report.errors += 1
            return walk
        if stat.S_ISREG(st.st_mode):
            parent = relpath.rsplit("/", 1)[0] if "/" in relpath else ""
            settings = self._effective(policy, library_id, parent)
            walk.files[relpath] = _DiskFile(
                st.st_size,
                st.st_mtime_ns,
                st.st_ino,
                settings.hash,
                self._secret(settings, relpath),
            )
            return walk
        if not stat.S_ISDIR(st.st_mode):
            return walk  # symlink, socket, device...
        if self._effective(policy, library_id, relpath).index == "none":
            return walk

        walk.dirs[relpath] = st.st_ino
        stack = [relpath]
        while stack:
            folder = stack.pop()
            settings = self._effective(policy, library_id, folder)
            patterns = self._patterns(policy, library_id, folder)
            try:
                with os.scandir(root / folder if folder else root) as it:
                    entries = list(it)
            except FileNotFoundError:
                continue
            except OSError:
                walk.unreadable.append(folder)
                report.errors += 1
                continue
            for entry in entries:
                if not _valid_name(entry.name):
                    report.errors += 1
                    continue
                child = f"{folder}/{entry.name}" if folder else entry.name
                if is_ignored(child, patterns):
                    continue
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if self._effective(policy, library_id, child).index != "none":
                            walk.dirs[child] = entry.inode()
                            stack.append(child)
                    elif entry.is_file(follow_symlinks=False):
                        s = entry.stat(follow_symlinks=False)
                        walk.files[child] = _DiskFile(
                            s.st_size,
                            s.st_mtime_ns,
                            s.st_ino,
                            settings.hash,
                            self._secret(settings, child),
                        )
                except FileNotFoundError:
                    continue
                except OSError:
                    report.errors += 1
            self._tick()
        return walk

    # --- folders --------------------------------------------------------------------

    def _folders(self, library_id: str, root: Path, walk: _Walk, report: ScanReport) -> None:
        file_inodes = {f.inode: path for path, f in walk.files.items()}
        ordered = sorted(walk.dirs.items(), key=lambda kv: (kv[0].count("/"), kv[0]))
        for batch in _chunks(ordered, BATCH):
            now = self.clock()
            with self.dbs.state.write() as conn:
                for relpath, inode in batch:
                    row = conn.execute(
                        select(folders.c.folder_id).where(
                            folders.c.library_id == library_id, folders.c.relpath == relpath
                        )
                    ).first()
                    if row is None:
                        source = self._folder_source(
                            conn, library_id, root, relpath, inode, file_inodes, now
                        )
                        if source is not None:
                            try:
                                folder_store.move_folder(
                                    conn,
                                    FolderRef(library_id, source),
                                    relpath,
                                    actor=self.actor,
                                    now=now,
                                )
                                report.folders_moved += 1
                            except FolderError:
                                log.warning("folder move not applied", exc_info=True)
                        else:
                            report.folders_new += 1
                        folder_store.ensure_folder(conn, FolderRef(library_id, relpath), now=now)
                    conn.execute(
                        update(folders)
                        .where(folders.c.library_id == library_id, folders.c.relpath == relpath)
                        .values(
                            present=True,
                            inode=inode,
                            first_seen=func.coalesce(
                                folders.c.first_seen, literal(now, UTCDateTime)
                            ),
                            last_seen=now,
                            missing_since=None,
                            updated_at=now,
                        )
                    )
            self._tick()

    def _folder_source(
        self,
        conn: Connection,
        library_id: str,
        root: Path,
        relpath: str,
        inode: int,
        file_inodes: dict[int, str],
        now: datetime,
    ) -> str | None:
        """The old path of a folder that was renamed or moved to `relpath`, if any."""
        candidates = conn.execute(
            select(folders.c.relpath, folders.c.present, folders.c.missing_since).where(
                folders.c.library_id == library_id,
                folders.c.inode == inode,
                folders.c.relpath != relpath,
                folders.c.relpath != "",
            )
        )
        for c in candidates:
            if is_within(relpath, c.relpath) or _exists(root, c.relpath):
                continue
            if not c.present and not self._recent(c.missing_since, now):
                continue
            had = conn.execute(
                select(files.c.inode)
                .where(*_under(files, library_id, c.relpath), files.c.inode.is_not(None))
                .limit(5000)
            ).scalars()
            had_inodes = set(had)
            if had_inodes and not any(
                i in file_inodes and is_within(file_inodes[i], relpath) for i in had_inodes
            ):
                continue
            return str(c.relpath)
        return None

    # --- files ------------------------------------------------------------------------

    def _files(
        self,
        policy: Policy,
        library_id: str,
        root: Path,
        scope: str,
        walk: _Walk,
        report: ScanReport,
    ) -> None:
        with self.dbs.state.read() as conn:
            rows = {
                r.relpath: r
                for r in conn.execute(select(files).where(*_under(files, library_id, scope)))
            }
        unchanged: list[str] = []
        for relpath, disk in sorted(walk.files.items()):
            row = rows.get(relpath)
            override = self._overrides.get(row.file_id) if row is not None else None
            if disk.excluded or override == "excluded":
                if row is not None and row.present and _same(row, disk) and not row.content_hash:
                    unchanged.append(row.file_id)
                    report.files_unchanged += 1
                else:
                    self._excluded_file(policy, library_id, relpath, row, disk, report)
                self._tick()
                continue
            if row is not None and _same(row, disk) and _hash_ok(row, disk):
                if row.present:
                    unchanged.append(row.file_id)
                    report.files_unchanged += 1
                    if len(unchanged) >= BATCH:
                        self._touch(unchanged)
                        unchanged = []
                    continue
                self._reappeared(row, disk, None, report)
            elif row is not None:
                result = self._hash(root / relpath, disk, report)
                if result is None:
                    if self._last_error is not None:
                        self._record_error(row.file_id, self._last_error)
                elif row.present:
                    self._changed(row, disk, result, report)
                else:
                    self._reappeared(row, disk, result, report)
            else:
                self._new_path(policy, library_id, root, relpath, disk, report)
            self._tick()
        self._touch(unchanged)

    def _excluded_file(
        self,
        policy: Policy,
        library_id: str,
        relpath: str,
        row: Any,
        disk: _DiskFile,
        report: ScanReport,
    ) -> None:
        """Record an excluded file without reading it: no hash, and any hash it had
        before it became excluded is dropped (the blob stays for other copies)."""
        now = self.clock()
        if row is not None:
            with self.dbs.state.write() as conn:
                self._update_row(conn, row.file_id, disk, None, now, clear_hash=True)
                event = "file.changed" if row.present else "file.reappeared"
                self._emit(conn, event, row.file_id, library_id, relpath, None, now)
            if row.present:
                report.files_changed += 1
            else:
                report.files_reappeared += 1
            return
        with self.dbs.state.read() as conn:
            source = self._inode_source(conn, policy, library_id, relpath, disk, now)
        if source is not None:
            self._move(source, library_id, relpath, disk, None, report, clear_hash=True)
        else:
            self._insert(library_id, relpath, disk, None, report, announce=True)

    def _hash(self, path: Path, disk: _DiskFile, report: ScanReport) -> HashResult | None:
        """Hash a file; None if it vanished, changed while being read, or can't be read
        (then `_last_error` names the error)."""
        self._last_error = None
        try:
            result = hash_file(path, disk.hash_policy, throttle=self.throttle)
            after = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            report.errors += 1
            self._last_error = type(exc).__name__
            return None
        if (after.st_size, after.st_mtime_ns, after.st_ino) != (
            disk.size,
            disk.mtime_ns,
            disk.inode,
        ) or result.size != disk.size:
            report.unstable += 1
            return None
        self._record_blob(result)
        return result

    def _record_blob(self, result: HashResult) -> None:
        now = self.clock()
        with self.dbs.index.write() as conn:
            conn.execute(
                insert(blobs)
                .values(
                    content_hash=result.content_hash,
                    hash_kind=result.hash_kind,
                    size=result.size,
                    mime=result.mime,
                    kind=result.kind,
                    created_at=now,
                )
                .on_conflict_do_nothing()
            )
            conn.execute(
                insert(stage_runs)
                .values(
                    content_hash=result.content_hash,
                    stage=HASH_STAGE,
                    stage_version=HASH_STAGE_VERSION,
                    status="done",
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=[stage_runs.c.content_hash, stage_runs.c.stage],
                    set_={"stage_version": HASH_STAGE_VERSION, "status": "done", "error": None},
                )
            )

    def _new_path(
        self,
        policy: Policy,
        library_id: str,
        root: Path,
        relpath: str,
        disk: _DiskFile,
        report: ScanReport,
    ) -> None:
        now = self.clock()
        with self.dbs.state.read() as conn:
            source = self._inode_source(conn, policy, library_id, relpath, disk, now)
        if source is not None and _hash_ok(source, disk):
            self._move(source, library_id, relpath, disk, None, report)
            return
        result = self._hash(root / relpath, disk, report)
        if result is None:
            if self._last_error is not None:
                self._insert(library_id, relpath, disk, None, report, error=self._last_error)
            return
        if source is None:
            with self.dbs.state.read() as conn:
                source = self._content_source(conn, policy, relpath, disk, result, now)
        if source is not None:
            self._move(source, library_id, relpath, disk, result, report)
        else:
            self._insert(library_id, relpath, disk, result, report)

    def _vanished(self, policy: Policy, row: Any, now: datetime) -> bool:
        library = policy.libraries.get(row.library_id)
        if library is None or _exists(Path(library.root), row.relpath):
            return False
        return bool(row.present) or self._recent(row.missing_since, now)

    def _inode_source(
        self,
        conn: Connection,
        policy: Policy,
        library_id: str,
        relpath: str,
        disk: _DiskFile,
        now: datetime,
    ) -> Any:
        """A vanished file in this library with the same inode, size and mtime."""
        for row in conn.execute(
            select(files).where(
                files.c.library_id == library_id,
                files.c.inode == disk.inode,
                files.c.size == disk.size,
                files.c.mtime_ns == disk.mtime_ns,
                files.c.relpath != relpath,
            )
        ):
            if self._vanished(policy, row, now):
                return row
        return None

    def _content_source(
        self,
        conn: Connection,
        policy: Policy,
        relpath: str,
        disk: _DiskFile,
        result: HashResult,
        now: datetime,
    ) -> Any:
        """A vanished file anywhere with the same content (best match first)."""
        query = select(files).where(files.c.content_hash == result.content_hash)
        if result.hash_kind == "sampled":
            query = query.where(files.c.size == disk.size, files.c.mtime_ns == disk.mtime_ns)
        name = relpath.rsplit("/", 1)[-1]
        candidates = [row for row in conn.execute(query) if self._vanished(policy, row, now)]
        candidates.sort(key=lambda r: (r.relpath.rsplit("/", 1)[-1] != name, r.relpath))
        return candidates[0] if candidates else None

    # --- writing file rows -------------------------------------------------------------

    def _insert(
        self,
        library_id: str,
        relpath: str,
        disk: _DiskFile,
        result: HashResult | None,
        report: ScanReport,
        *,
        error: str | None = None,
        announce: bool = False,
    ) -> None:
        """`announce`: emit `file.discovered` even without a hash (excluded files)."""
        now = self.clock()
        file_id = uuid.uuid4().hex
        with self.dbs.state.write() as conn:
            conn.execute(
                files.insert().values(
                    file_id=file_id,
                    library_id=library_id,
                    relpath=relpath,
                    content_hash=result.content_hash if result else None,
                    hash_kind=result.hash_kind if result else None,
                    size=disk.size,
                    mtime_ns=disk.mtime_ns,
                    inode=disk.inode,
                    present=True,
                    first_seen=now,
                    last_seen=now,
                    error=error,
                    created_at=now,
                    updated_at=now,
                )
            )
            if result is not None or announce:
                self._emit(conn, "file.discovered", file_id, library_id, relpath, result, now)
        if result is not None or announce:
            report.files_new += 1

    def _changed(self, row: Any, disk: _DiskFile, result: HashResult, report: ScanReport) -> None:
        now = self.clock()
        with self.dbs.state.write() as conn:
            self._update_row(conn, row.file_id, disk, result, now)
            if result.content_hash != row.content_hash:
                self._emit(
                    conn,
                    "file.changed",
                    row.file_id,
                    row.library_id,
                    row.relpath,
                    result,
                    now,
                    old_content_hash=row.content_hash,
                )
                report.files_changed += 1
            else:
                report.files_unchanged += 1

    def _reappeared(
        self, row: Any, disk: _DiskFile, result: HashResult | None, report: ScanReport
    ) -> None:
        now = self.clock()
        with self.dbs.state.write() as conn:
            self._update_row(conn, row.file_id, disk, result, now)
            self._emit(
                conn,
                "file.reappeared",
                row.file_id,
                row.library_id,
                row.relpath,
                result,
                now,
                content_hash=result.content_hash if result else row.content_hash,
                old_content_hash=row.content_hash,
            )
        report.files_reappeared += 1

    def _move(
        self,
        row: Any,
        library_id: str,
        relpath: str,
        disk: _DiskFile,
        result: HashResult | None,
        report: ScanReport,
        *,
        clear_hash: bool = False,
    ) -> None:
        now = self.clock()
        with self.dbs.state.write() as conn:
            conn.execute(
                update(files)
                .where(files.c.file_id == row.file_id)
                .values(library_id=library_id, relpath=relpath)
            )
            self._update_row(conn, row.file_id, disk, result, now, clear_hash=clear_hash)
            conn.execute(
                path_history.insert().values(
                    file_id=row.file_id,
                    old_library_id=row.library_id,
                    old_relpath=row.relpath,
                    new_library_id=library_id,
                    new_relpath=relpath,
                    actor=self.actor,
                    changed_at=now,
                )
            )
            self._emit(
                conn,
                "file.moved",
                row.file_id,
                library_id,
                relpath,
                result,
                now,
                content_hash=result.content_hash
                if result
                else (None if clear_hash else row.content_hash),
                old_library_id=row.library_id,
                old_relpath=row.relpath,
            )
        report.files_moved += 1

    def _update_row(
        self,
        conn: Connection,
        file_id: str,
        disk: _DiskFile,
        result: HashResult | None,
        now: datetime,
        *,
        clear_hash: bool = False,
    ) -> None:
        values: dict[str, Any] = {
            "size": disk.size,
            "mtime_ns": disk.mtime_ns,
            "inode": disk.inode,
            "present": True,
            "last_seen": now,
            "missing_since": None,
            "updated_at": now,
        }
        if result is not None:
            values.update(content_hash=result.content_hash, hash_kind=result.hash_kind, error=None)
        elif clear_hash:
            values.update(content_hash=None, hash_kind=None, error=None)
        conn.execute(update(files).where(files.c.file_id == file_id).values(**values))

    def _record_error(self, file_id: str, error: str) -> None:
        now = self.clock()
        with self.dbs.state.write() as conn:
            conn.execute(
                update(files)
                .where(files.c.file_id == file_id)
                .values(error=error, last_seen=now, updated_at=now)
            )

    def _touch(self, file_ids: list[str]) -> None:
        if not file_ids:
            return
        now = self.clock()
        with self.dbs.state.write() as conn:
            conn.execute(update(files).where(files.c.file_id.in_(file_ids)).values(last_seen=now))

    def _emit(
        self,
        conn: Connection,
        event_type: str,
        file_id: str,
        library_id: str,
        relpath: str,
        result: HashResult | None,
        now: datetime,
        **extra: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "file_id": file_id,
            "library_id": library_id,
            "relpath": relpath,
        }
        if result is not None:
            payload.update(content_hash=result.content_hash, size=result.size, mime=result.mime)
        payload.update(extra)
        events.emit(conn, event_type, payload, actor=self.actor, now=now)

    # --- missing ------------------------------------------------------------------------

    def _missing(self, library_id: str, scope: str, walk: _Walk, report: ScanReport) -> None:
        now = self.clock()
        with self.dbs.state.read() as conn:
            gone_folders = [
                r.folder_id
                for r in conn.execute(
                    select(folders.c.folder_id, folders.c.relpath).where(
                        *_under(folders, library_id, scope), folders.c.present.is_(True)
                    )
                )
                if r.relpath not in walk.dirs and walk.readable(r.relpath)
            ]
            gone_files = [
                r
                for r in conn.execute(
                    select(files.c.file_id, files.c.relpath, files.c.content_hash).where(
                        *_under(files, library_id, scope), files.c.present.is_(True)
                    )
                )
                if r.relpath not in walk.files and walk.readable(r.relpath)
            ]
        for batch in _chunks(gone_folders, BATCH):
            with self.dbs.state.write() as conn:
                conn.execute(
                    update(folders)
                    .where(folders.c.folder_id.in_(batch))
                    .values(present=False, missing_since=now, updated_at=now)
                )
        report.folders_missing += len(gone_folders)
        for rows in _chunks(gone_files, BATCH):
            with self.dbs.state.write() as conn:
                conn.execute(
                    update(files)
                    .where(files.c.file_id.in_([r.file_id for r in rows]))
                    .values(present=False, missing_since=now, updated_at=now)
                )
                for r in rows:
                    events.emit(
                        conn,
                        "file.missing",
                        {
                            "file_id": r.file_id,
                            "library_id": library_id,
                            "relpath": r.relpath,
                            "content_hash": r.content_hash,
                        },
                        actor=self.actor,
                        now=now,
                    )
        report.files_missing += len(gone_files)

    # --- helpers --------------------------------------------------------------------------

    def _recent(self, missing_since: datetime | None, now: datetime) -> bool:
        window = timedelta(hours=self.settings.move_window_hours)
        return missing_since is not None and missing_since >= now - window

    def _tick(self) -> None:
        if self.renew is not None and time.monotonic() - self._renewed > RENEW_EVERY_SECONDS:
            self.renew()
            self._renewed = time.monotonic()


def _same(row: Any, disk: _DiskFile) -> bool:
    return (row.size, row.mtime_ns, row.inode) == (disk.size, disk.mtime_ns, disk.inode)


def _hash_ok(row: Any, disk: _DiskFile) -> bool:
    """The row has a hash at least as strong as the folder's hash policy asks for."""
    if row.content_hash is None:
        return False
    return bool(row.hash_kind == "full" or disk.hash_policy == "sampled")


def _under(table: Any, library_id: str, relpath: str) -> tuple[Any, ...]:
    if relpath == "":
        return (table.c.library_id == library_id,)
    prefix = relpath + "/"
    return (
        table.c.library_id == library_id,
        (table.c.relpath == relpath) | (func.substr(table.c.relpath, 1, len(prefix)) == prefix),
    )


def _exists(root: Path, relpath: str) -> bool:
    return os.path.lexists(root / relpath) if relpath else root.is_dir()


def _empty_dir(path: Path) -> bool:
    with os.scandir(path) as it:
        return next(it, None) is None


def _valid_name(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _outermost(paths: Iterable[str]) -> list[str]:
    """Drop paths inside other paths in the list."""
    unique = sorted(set(paths))
    return [p for p in unique if not any(q != p and is_within(p, q) for q in unique)]


def _chunks[T](items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
