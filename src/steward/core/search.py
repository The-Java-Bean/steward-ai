# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Filename and metadata search (Phase 1's keyword search; content search comes with the
`search` module).

Every word of the query must appear in the file's path (case-insensitive); files whose
*name* contains the words rank first, then newer files. Filters: library, folder, kind,
size and modification time.

What can be found depends on the scope, and is decided **in the SQL query**:
- `normal`: never anything in a locked folder, a hidden folder (`visible: false`), a
  folder holding copies of other files (trash, edit backups, `trash`-type folders), or
  steward-ai's own `.steward` folder.
- `locked`: only files in locked folders (the Locked Folder view, after a re-auth).
"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from steward.core.db import Databases
from steward.core.folders.paths import FolderRef, PathError
from steward.core.folders.policy import COPY_HOLDING_ROLES, Policy
from steward.core.folders.store import STEWARD_DIR

type Scope = Literal["normal", "locked"]

MAX_TOKENS = 10
MAX_TOKEN_LENGTH = 200
MAX_LIMIT = 200
COUNT_CAP = 10_000


class SearchError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SearchFilters:
    library: str | None = None
    folder: str | None = None
    """`library:relative/path`: only files inside this folder."""
    kind: str | None = None
    """`document`, `image`, `video`, `audio`, `text`, `archive`, `other`."""
    min_size: int | None = None
    max_size: int | None = None
    modified_after: datetime | None = None
    modified_before: datetime | None = None


@dataclass(frozen=True, slots=True)
class SearchHit:
    file_id: str
    library_id: str
    relpath: str
    size: int
    modified: datetime
    kind: str | None
    mime: str | None

    @property
    def name(self) -> str:
        return self.relpath.rsplit("/", 1)[-1]

    @property
    def folder(self) -> str:
        return self.relpath.rsplit("/", 1)[0] if "/" in self.relpath else ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "library_id": self.library_id,
            "path": f"{self.library_id}:{self.relpath}",
            "name": self.name,
            "size": self.size,
            "modified": self.modified.isoformat(),
            "kind": self.kind,
            "mime": self.mime,
        }


@dataclass(frozen=True, slots=True)
class SearchResults:
    hits: list[SearchHit]
    total: int
    """Number of matches, counted up to `COUNT_CAP`."""
    capped: bool


def fold(text: str | None) -> str | None:
    return unicodedata.normalize("NFC", text).casefold() if text is not None else None


def tokens(query: str) -> list[str]:
    words = [w for w in (fold(query) or "").split() if w]
    if len(words) > MAX_TOKENS:
        raise SearchError(f"use at most {MAX_TOKENS} words")
    if any(len(w) > MAX_TOKEN_LENGTH for w in words):
        raise SearchError("a word is too long")
    return words


def hidden_folders(policy: Policy) -> tuple[list[FolderRef], list[FolderRef]]:
    """(folders hidden from normal search, locked folders). Restrictions carry down, so
    the outermost folder of each is enough."""
    hidden: set[FolderRef] = set()
    locked: set[FolderRef] = set()
    candidates = {*policy.mappings, *policy.roles.values()}
    candidates.update(FolderRef(lib, "") for lib in policy.libraries)
    # steward-ai's own folder in each library (default trash and edit backups).
    hidden.update(FolderRef(lib, STEWARD_DIR) for lib in policy.libraries)
    for ref in candidates:
        if ref.library_id not in policy.libraries:
            continue
        try:
            settings = policy.settings_for(ref)
        except PathError:
            continue
        if settings.locked and settings.locked_by == ref.relpath:
            locked.add(ref)
        if not settings.visible or settings.type.name == "trash":
            hidden.add(ref)
    for role in COPY_HOLDING_ROLES:
        if role in policy.roles:
            hidden.add(policy.roles[role])
    return _outermost(hidden | locked), _outermost(locked)


class Visibility:
    """Which paths normal views may show (one policy snapshot)."""

    def __init__(self, policy: Policy) -> None:
        self.hidden, self.locked = hidden_folders(policy)

    def is_hidden(self, library_id: str, relpath: str) -> bool:
        """Inside a locked, hidden or copy-holding folder."""
        return any(_inside(ref, library_id, relpath) for ref in self.hidden)

    def is_locked(self, library_id: str, relpath: str) -> bool:
        return any(_inside(ref, library_id, relpath) for ref in self.locked)

    def path_hidden(self, path: str) -> bool:
        library_id, _, relpath = path.partition(":")
        return self.is_hidden(library_id, relpath)

    def path_locked(self, path: str) -> bool:
        library_id, _, relpath = path.partition(":")
        return self.is_locked(library_id, relpath)


def _inside(ref: FolderRef, library_id: str, relpath: str) -> bool:
    return ref.library_id == library_id and (
        ref.relpath == "" or relpath == ref.relpath or relpath.startswith(ref.relpath + "/")
    )


def _outermost(refs: set[FolderRef]) -> list[FolderRef]:
    return sorted(
        r
        for r in refs
        if not any(
            o != r
            and o.library_id == r.library_id
            and (o.relpath == "" or r.relpath.startswith(o.relpath + "/"))
            for o in refs
        )
    )


def _under(ref: FolderRef) -> tuple[str, list[Any]]:
    if ref.relpath == "":
        return "(f.library_id = ?)", [ref.library_id]
    prefix = ref.relpath + "/"
    return (
        "(f.library_id = ? AND (f.relpath = ? OR substr(f.relpath, 1, ?) = ?))",
        [ref.library_id, ref.relpath, len(prefix), prefix],
    )


@contextmanager
def _connect(dbs: Databases) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(f"file:{dbs.state.path}?mode=ro", uri=True, timeout=10)
    try:
        conn.execute("ATTACH DATABASE ? AS idx", (f"file:{dbs.index.path}?mode=ro",))
        conn.create_function("steward_fold", 1, fold, deterministic=True)
        conn.create_function(
            "steward_name",
            1,
            lambda p: fold(p.rsplit("/", 1)[-1]) if p is not None else None,
            deterministic=True,
        )
        yield conn
    finally:
        conn.close()


def search_files(
    dbs: Databases,
    policy: Policy,
    query: str,
    *,
    filters: SearchFilters = SearchFilters(),  # noqa: B008 -- frozen dataclass
    scope: Scope = "normal",
    limit: int = 50,
    offset: int = 0,
) -> SearchResults:
    words = tokens(query)
    if not 1 <= limit <= MAX_LIMIT or offset < 0:
        raise SearchError(f"limit must be 1-{MAX_LIMIT} and offset at least 0")
    where = ["f.present = 1"]
    args: list[Any] = []
    for word in words:
        where.append("instr(steward_fold(f.relpath), ?) > 0")
        args.append(word)

    hidden, locked = hidden_folders(policy)
    if scope == "normal":
        for ref in hidden:
            clause, values = _under(ref)
            where.append(f"NOT {clause}")
            args.extend(values)
    else:
        if not locked:
            return SearchResults([], 0, capped=False)
        parts = [_under(ref) for ref in locked]
        where.append("(" + " OR ".join(c for c, _ in parts) + ")")
        for _, values in parts:
            args.extend(values)

    if filters.library:
        where.append("f.library_id = ?")
        args.append(filters.library)
    if filters.folder:
        try:
            folder = FolderRef.parse(filters.folder)
        except PathError as exc:
            raise SearchError(str(exc)) from None
        clause, values = _under(folder)
        where.append(clause)
        args.extend(values)
    if filters.kind:
        where.append("b.kind = ?")
        args.append(filters.kind)
    if filters.min_size is not None:
        where.append("f.size >= ?")
        args.append(filters.min_size)
    if filters.max_size is not None:
        where.append("f.size <= ?")
        args.append(filters.max_size)
    if filters.modified_after is not None:
        where.append("f.mtime_ns >= ?")
        args.append(_ns(filters.modified_after))
    if filters.modified_before is not None:
        where.append("f.mtime_ns < ?")
        args.append(_ns(filters.modified_before))

    sql_from = (
        "FROM files f LEFT JOIN idx.blobs b ON b.content_hash = f.content_hash "
        f"WHERE {' AND '.join(where)}"
    )
    name_score = " + ".join("(instr(steward_name(f.relpath), ?) > 0)" for _ in words)
    order = f"({name_score}) DESC, " if words else ""
    with _connect(dbs) as conn:
        total = conn.execute(
            f"SELECT count(*) FROM (SELECT 1 {sql_from} LIMIT {COUNT_CAP + 1})",  # noqa: S608 -- only placeholders are interpolated
            args,
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT f.file_id, f.library_id, f.relpath, f.size, f.mtime_ns, b.kind, b.mime "
            f"{sql_from} ORDER BY {order}f.mtime_ns DESC, f.relpath LIMIT ? OFFSET ?",
            [*args, *words, limit, offset],
        ).fetchall()
    hits = [
        SearchHit(
            file_id=r[0],
            library_id=r[1],
            relpath=r[2],
            size=r[3],
            modified=datetime.fromtimestamp(r[4] / 1e9, tz=UTC),
            kind=r[5],
            mime=r[6],
        )
        for r in rows
    ]
    return SearchResults(hits, min(total, COUNT_CAP), capped=total > COUNT_CAP)


def _ns(value: datetime) -> int:
    if value.tzinfo is None:
        raise SearchError("dates must include a timezone")
    return int(value.timestamp() * 1e9)
