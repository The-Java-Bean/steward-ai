# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Reading libraries for people: folder listings, file opens and zip members.

Listings come from the disk (so files discovery hasn't seen yet still show) merged with
`state.db` (file IDs) and `index.db` (types). Every path is opened one component at a
time without following symlinks, the same way the executor does, and checked against
the scope: normal views never reach locked, hidden or trash folders; the `locked` scope
reaches only locked folders; the `shared` scope (public share links) reaches only one
shared item and what inside it may be shared. Nothing here writes.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import os
import stat
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select

from steward.core import content
from steward.core.db import Databases
from steward.core.db.schema import blobs, files, folders
from steward.core.discovery.hashing import HEAD, detect_mime, kind_of
from steward.core.executor.guarded import (
    TEMP_PREFIX,
    FolderMissing,
    check_segments,
    open_dir,
    split_path,
)
from steward.core.folders import FolderRef, PathError
from steward.core.folders.policy import FolderMapping, Policy
from steward.core.privacy.sharing import SharingRules
from steward.core.search import Embed, SearchError, SearchFilters, Visibility, search_all
from steward.core.tags import effective
from steward.sdk import SIDECAR_SUFFIX, ActionRefused, is_sidecar
from steward.sdk.web import (
    Crumb,
    Entry,
    FileDetails,
    FilePage,
    FolderListing,
    LibraryInfo,
    LockedFolder,
    NotFound,
    Scope,
    UnlockPreview,
)

READ_CHUNK = 256 * 1024
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
_IN_BATCH = 500
UNLOCK_SAMPLE = 10

type BrowseScope = Literal["normal", "locked", "shared"]
"""The SDK's scopes plus `shared` (only for the core's share-link handling)."""


@dataclass(frozen=True, slots=True)
class SharedArea:
    """What the `shared` scope may reach: `relpath` in `library_id` and what inside it
    may be shared; with `allowed` (a snapshot) only those relpaths."""

    library_id: str
    relpath: str
    sharing: SharingRules
    allowed: frozenset[str] | None = None

    def allows(self, library_id: str, relpath: str) -> bool:
        if library_id != self.library_id:
            return False
        root = self.relpath
        if not (root == "" or relpath == root or relpath.startswith(root + "/")):
            return False
        if self.allowed is not None and relpath not in self.allowed:
            return False
        if is_sidecar(relpath):
            return False  # they hold AI tags and descriptions the owner didn't choose to share
        return self.sharing.check(library_id, relpath).shareable


@dataclass(frozen=True, slots=True)
class Located:
    library_id: str
    relpath: str
    root: str

    @property
    def path(self) -> str:
        return f"{self.library_id}:{self.relpath}"

    @property
    def parts(self) -> list[str]:
        return self.relpath.split("/") if self.relpath else []

    @property
    def name(self) -> str:
        return self.relpath.rsplit("/", 1)[-1]


@dataclass(slots=True)
class OpenFile:
    """An open regular file (close it, or use it as a context manager)."""

    fd: int
    entry: Entry
    size: int

    def pread(self, length: int, offset: int) -> bytes:
        return os.pread(self.fd, length, offset)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> OpenFile:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class ZipMember:
    """One entry of a zip: a folder (`open` is None) or a file to read."""

    arcname: str
    modified: datetime
    location: Located | None = None


CAPTION_ARTIFACT = "text:caption"
"""The text artifact shown as a file's description (written by `photos.caption`)."""


class Browser:
    """Read access to the libraries for one policy snapshot."""

    def __init__(
        self,
        dbs: Databases,
        policy: Policy,
        shared: SharedArea | None = None,
        embed: Embed | None = None,
    ) -> None:
        self.dbs = dbs
        self.policy = policy
        self.visibility = Visibility(policy)
        self.shared = shared
        self.embed = embed
        """Embeds queries for searching by meaning (None: words and names only)."""

    # --- paths and scope ---------------------------------------------------------------

    def libraries(self) -> list[LibraryInfo]:
        return [
            LibraryInfo(lib.library_id, lib.name)
            for lib in sorted(self.policy.libraries.values(), key=lambda lib: lib.position)
        ]

    def locate(self, path: str, scope: BrowseScope) -> Located:
        """Parse a `library:path` and check it is visible in `scope` (else `NotFound`)."""
        try:
            library_id, relpath = split_path(path)
            check_segments(relpath)
        except ActionRefused:
            raise NotFound(path) from None
        library = self.policy.libraries.get(library_id)
        if library is None or not self.visible(library_id, relpath, scope):
            raise NotFound(path)
        return Located(library_id, relpath, library.root)

    def visible(self, library_id: str, relpath: str, scope: BrowseScope) -> bool:
        if relpath.startswith(TEMP_PREFIX) or f"/{TEMP_PREFIX}" in relpath:
            return False
        if scope == "locked":
            return self.visibility.in_locked_view(library_id, relpath)
        if scope == "shared":
            return self.shared is not None and self.shared.allows(library_id, relpath)
        return not self.visibility.is_hidden(library_id, relpath)

    def locked_folders(self) -> list[LockedFolder]:
        found: list[tuple[int, str, LockedFolder]] = []
        for ref in self.visibility.locked_roots():
            library = self.policy.libraries[ref.library_id]
            settings = self.policy.settings_for(ref)
            name = ref.relpath.rsplit("/", 1)[-1] if ref.relpath else library.name
            folder = LockedFolder(
                path=str(ref),
                name=name,
                library_name=library.name,
                permanent=settings.permanent,
                type_name=settings.type.name,
            )
            found.append((library.position, ref.relpath, folder))
        return [f for _, _, f in sorted(found, key=lambda t: (t[0], t[1]))]

    # --- listing -------------------------------------------------------------------------

    def listing(self, path: str, *, scope: BrowseScope, limit: int) -> FolderListing:
        where = self.locate(path, scope)
        try:
            settings = self.policy.settings_for(FolderRef(where.library_id, where.relpath))
        except PathError:
            raise NotFound(path) from None
        raw: list[tuple[str, os.stat_result]] = []
        truncated = False
        with self._dir(where) as fd, os.scandir(fd) as it:
            for item in it:
                child = f"{where.relpath}/{item.name}" if where.relpath else item.name
                if not self.visible(where.library_id, child, scope):
                    continue
                try:
                    st = item.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                    continue  # symlinks, sockets...: never shown or followed
                if len(raw) >= limit:
                    truncated = True
                    break
                raw.append((item.name, st))
        names = {name for name, _ in raw}
        raw = [
            (name, st)
            for name, st in raw
            if not (is_sidecar(name) and name[: -len(SIDECAR_SUFFIX)] in names)
        ]
        known = self._known(where.library_id, [_join(where.relpath, n) for n, _ in raw])
        entries = [
            self._entry(where.library_id, _join(where.relpath, name), st, known, scope)
            for name, st in raw
        ]
        entries.sort(key=lambda e: (e.kind != "dir", e.name.casefold(), e.name))
        return FolderListing(
            path=where.path,
            library_id=where.library_id,
            relpath=where.relpath,
            name=where.name or self.policy.libraries[where.library_id].name,
            crumbs=self._crumbs(where, scope),
            entries=tuple(entries),
            type_name=settings.type.name,
            writable=settings.writable,
            shareable=settings.shareable,
            locked=settings.locked,
            permanent=settings.permanent,
            truncated=truncated,
            lock_from=(
                f"{where.library_id}:{settings.locked_by}"
                if settings.locked and settings.locked_by is not None
                else None
            ),
        )

    def entry(self, path: str, *, scope: BrowseScope) -> Entry:
        where = self.locate(path, scope)
        if where.relpath == "":
            return Entry(self.policy.libraries[where.library_id].name, where.path, "dir")
        st = self._stat(where)
        return self._entry(
            where.library_id,
            where.relpath,
            st,
            self._known(where.library_id, [where.relpath]),
            scope,
        )

    def details(self, path: str, *, scope: BrowseScope) -> FileDetails:
        where = self.locate(path, scope)
        if where.relpath == "" or not stat.S_ISREG(self._stat(where).st_mode):
            raise NotFound(where.path)
        with self.dbs.state.read() as conn:
            row = conn.execute(
                select(files.c.file_id, files.c.content_hash).where(
                    files.c.library_id == where.library_id,
                    files.c.relpath == where.relpath,
                    files.c.present.is_(True),
                )
            ).one_or_none()
        if row is None:
            return FileDetails()
        with self.dbs.state.read() as state, self.dbs.index.read() as index:
            tags = tuple(
                effective.file_tags(
                    state,
                    index,
                    self.policy,
                    file_id=row.file_id,
                    content_hash=row.content_hash,
                    library_id=where.library_id,
                    relpath=where.relpath,
                )
            )
            if row.content_hash is None:
                return FileDetails(tags=tags)
            caption = content.read_text(index, row.content_hash, CAPTION_ARTIFACT)
            props = content.properties_of(index, row.content_hash)
        description = " ".join(p.body for p in caption).strip() or None
        return FileDetails(description=description, properties=props, tags=tags)

    def by_kind(
        self, kind: str, *, library: str | None, scope: Scope, limit: int, offset: int
    ) -> FilePage:
        try:
            return self.search(
                "", kind=kind, library=library, scope=scope, limit=limit, offset=offset
            )
        except SearchError:
            return FilePage((), 0, False)

    def search(
        self,
        query: str,
        *,
        scope: Scope,
        folder: str | None = None,
        kind: str | None = None,
        library: str | None = None,
        limit: int,
        offset: int,
    ) -> FilePage:
        """Raises `SearchError` (a `ValueError`) for a query that can't be searched."""
        if folder is not None:
            where = self.locate(folder, scope)
            folder = where.path
        results = search_all(
            self.dbs,
            self.policy,
            query,
            filters=SearchFilters(kind=kind, library=library, folder=folder),
            scope=scope,
            limit=limit,
            offset=offset,
            embed=self.embed if self.shared is None else None,
        )
        entries = tuple(
            Entry(
                name=h.name,
                path=f"{h.library_id}:{h.relpath}",
                kind="file",
                size=h.size,
                modified=h.modified,
                file_id=h.file_id,
                mime=h.mime,
                file_kind=h.kind,
                snippet=h.snippet,
                page=h.page,
                semantic=h.semantic,
            )
            for h in results.hits
        )
        return FilePage(entries, results.total, results.capped)

    def unlock_preview(self, path: str) -> UnlockPreview:
        """What unlocking `path` (a folder in the locked scope) would make visible, worked
        out on a copy of the policy without that folder's lock."""
        where = self.locate(path, "locked")
        ref = FolderRef(where.library_id, where.relpath)
        settings = self.policy.settings_for(ref)
        reason: str | None = None
        if settings.locked_by != where.relpath:
            parent = FolderRef(where.library_id, settings.locked_by or "")
            reason = f"It is locked because {parent} is. Unlock that folder instead."
        elif settings.permanent:
            reason = (
                "It is permanently locked. Only `steward folders unlock --permanent "
                f"{ref}` on the server can unlock it."
            )
        mapping = self.policy.mappings.get(ref)
        type_name = mapping.type_name if mapping is not None else None
        type_locks = (
            self.policy.types[type_name].locked
            if type_name is not None
            else where.relpath == "" and settings.type.locked
        )
        unlocked = dataclasses.replace(
            self.policy,
            mappings={
                **self.policy.mappings,
                ref: FolderMapping(
                    mapping.folder_id if mapping is not None else 0, ref, type_name, False, False
                ),
            },
        )
        after = Visibility(unlocked)
        prefix = f"{where.relpath}/" if where.relpath else ""
        n_files = n_folders = size = 0
        sample: list[str] = []
        with self.dbs.state.read() as conn:
            rows = [
                ("dir", r.relpath, 0)
                for r in conn.execute(
                    select(folders.c.relpath).where(
                        folders.c.library_id == where.library_id, folders.c.present.is_(True)
                    )
                )
            ] + [
                ("file", r.relpath, r.size or 0)
                for r in conn.execute(
                    select(files.c.relpath, files.c.size).where(
                        files.c.library_id == where.library_id, files.c.present.is_(True)
                    )
                )
            ]
        for kind, relpath, nbytes in sorted(rows, key=lambda r: r[1]):
            if relpath == where.relpath or not relpath.startswith(prefix):
                continue
            if after.is_hidden(where.library_id, relpath):
                continue
            if kind == "dir":
                n_folders += 1
            else:
                n_files += 1
                size += nbytes
            if len(sample) < UNLOCK_SAMPLE:
                sample.append(f"{where.library_id}:{relpath}")
        still = tuple(
            str(r)
            for r in after.locked_roots()
            if r.library_id == where.library_id and (not prefix or r.relpath.startswith(prefix))
        )
        return UnlockPreview(
            path=where.path,
            allowed=reason is None,
            reason=reason,
            files=n_files,
            folders=n_folders,
            size=size,
            sample=tuple(sample),
            still_locked=still,
            type_locks=bool(type_locks),
        )

    # --- reading ---------------------------------------------------------------------------

    def open(self, path: str, *, scope: BrowseScope) -> OpenFile:
        """Open a regular file for reading. Raises `NotFound`."""
        where = self.locate(path, scope)
        if where.relpath == "":
            raise NotFound(path)
        with self._dir(where, parent=True) as dir_fd:
            fd = _open_regular(dir_fd, where.name, path)
        try:
            st = os.fstat(fd)
            known = self._known(where.library_id, [where.relpath])
            entry = self._entry(where.library_id, where.relpath, st, known, scope, fd=fd)
        except BaseException:
            os.close(fd)
            raise
        return OpenFile(fd, entry, st.st_size)

    def zip_members(self, paths: Sequence[str], *, scope: BrowseScope) -> Iterator[ZipMember]:
        """Members for a zip of `paths` (files and whole folders), skipping whatever isn't
        visible in `scope`. Raises `NotFound` before yielding if a path isn't there."""
        located = [self.locate(p, scope) for p in paths]
        for where in located:
            if where.relpath:
                self._stat(where)
        return self._members(located, scope)

    def _members(self, located: list[Located], scope: BrowseScope) -> Iterator[ZipMember]:
        for where in located:
            top = where.name or self.policy.libraries[where.library_id].name
            st = self._stat(where) if where.relpath else None
            if st is not None and stat.S_ISREG(st.st_mode):
                yield ZipMember(top, _mtime(st.st_mtime_ns), where)
                continue
            yield from self._walk(where, top, scope)

    def _walk(self, where: Located, top: str, scope: BrowseScope) -> Iterator[ZipMember]:
        try:
            with self._dir(where) as fd:
                for dirpath, dirnames, filenames, dir_fd in os.fwalk(
                    ".", dir_fd=fd, follow_symlinks=False
                ):
                    inner = "" if dirpath == "." else dirpath.removeprefix("./")
                    base = _join(where.relpath, inner)
                    dirnames[:] = sorted(
                        d for d in dirnames if self.visible(where.library_id, _join(base, d), scope)
                    )
                    st = os.fstat(dir_fd)
                    yield ZipMember(f"{_join(top, inner)}/", _mtime(st.st_mtime_ns))
                    for name in sorted(filenames):
                        rel = _join(base, name)
                        if not self.visible(where.library_id, rel, scope):
                            continue
                        try:
                            fst = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                        except OSError:
                            continue
                        if stat.S_ISREG(fst.st_mode):
                            yield ZipMember(
                                _join(_join(top, inner), name),
                                _mtime(fst.st_mtime_ns),
                                Located(where.library_id, rel, where.root),
                            )
        except NotFound:
            return

    # --- helpers ------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _dir(self, where: Located, *, parent: bool = False) -> Iterator[int]:
        parts = where.parts[:-1] if parent else where.parts
        try:
            with open_dir(where.root, parts) as fd:
                yield fd
        except (FolderMissing, ActionRefused):
            raise NotFound(where.path) from None
        except NotADirectoryError:
            raise NotFound(where.path) from None

    def _stat(self, where: Located) -> os.stat_result:
        with self._dir(where, parent=True) as fd:
            try:
                st = os.stat(where.name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                raise NotFound(where.path) from None
        if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
            raise NotFound(where.path)
        return st

    def _known(self, library_id: str, relpaths: list[str]) -> dict[str, tuple[Any, Any]]:
        """relpath -> (files row, blobs row or None) for what discovery has recorded."""
        rows: list[Any] = []
        with self.dbs.state.read() as conn:
            for i in range(0, len(relpaths), _IN_BATCH):
                batch = relpaths[i : i + _IN_BATCH]
                rows.extend(
                    conn.execute(
                        select(files.c.relpath, files.c.file_id, files.c.content_hash).where(
                            files.c.library_id == library_id,
                            files.c.relpath.in_(batch),
                            files.c.present.is_(True),
                        )
                    ).all()
                )
        hashes = sorted({r.content_hash for r in rows if r.content_hash})
        found: dict[str, Any] = {}
        if hashes:
            with self.dbs.index.read() as conn:
                for i in range(0, len(hashes), _IN_BATCH):
                    for b in conn.execute(
                        select(blobs.c.content_hash, blobs.c.mime, blobs.c.kind).where(
                            blobs.c.content_hash.in_(hashes[i : i + _IN_BATCH])
                        )
                    ):
                        found[b.content_hash] = b
        return {r.relpath: (r, found.get(r.content_hash)) for r in rows}

    def _entry(
        self,
        library_id: str,
        relpath: str,
        st: os.stat_result,
        known: dict[str, tuple[Any, Any]],
        scope: BrowseScope,
        *,
        fd: int | None = None,
    ) -> Entry:
        name = relpath.rsplit("/", 1)[-1]
        path = f"{library_id}:{relpath}"
        if stat.S_ISDIR(st.st_mode):
            locked = scope == "locked" or self.visibility.is_locked(library_id, relpath)
            return Entry(name, path, "dir", modified=_mtime(st.st_mtime_ns), locked=locked)
        row, blob = known.get(relpath, (None, None))
        mime = blob.mime if blob is not None else None
        if mime is None:
            mime = detect_mime(name, os.pread(fd, HEAD, 0) if fd is not None else b"")
        return Entry(
            name=name,
            path=path,
            kind="file",
            size=st.st_size,
            modified=_mtime(st.st_mtime_ns),
            file_id=row.file_id if row is not None else None,
            mime=mime,
            file_kind=blob.kind if blob is not None else kind_of(mime),
        )

    def _crumbs(self, where: Located, scope: BrowseScope) -> tuple[Crumb, ...]:
        parts = where.parts
        if not parts:
            return ()
        crumbs: list[Crumb] = []
        if scope == "normal":
            crumbs.append(
                Crumb(self.policy.libraries[where.library_id].name, f"{where.library_id}:")
            )
        for i in range(1, len(parts)):
            rel = "/".join(parts[:i])
            if self.visible(where.library_id, rel, scope):
                crumbs.append(Crumb(parts[i - 1], f"{where.library_id}:{rel}"))
        return tuple(crumbs)


def _open_regular(dir_fd: int, name: str, path: str) -> int:
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ELOOP, errno.EISDIR, errno.ENOTDIR):
            raise NotFound(path) from None
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise NotFound(path)
    return fd


def open_member(member: ZipMember) -> int:
    """Open a zip member's file (`os.close` it). Raises `NotFound` if it went away."""
    assert member.location is not None  # noqa: S101 -- folders have no file
    where = member.location
    try:
        with open_dir(where.root, where.parts[:-1]) as dir_fd:
            return _open_regular(dir_fd, where.name, where.path)
    except (FolderMissing, ActionRefused):
        raise NotFound(where.path) from None


def _join(base: str, name: str) -> str:
    if not name:
        return base
    return f"{base}/{name}" if base else name


def _mtime(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, tz=UTC)
