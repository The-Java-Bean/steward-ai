# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Search: file names and paths, and the content index (extracted text).

- **Names** (`search_files`): every word of the query must appear in the file's path
  (case-insensitive); files whose *name* contains the words rank first, then newer files.
- **Text** (`search_text`): every word must appear in one chunk of the file's extracted
  text (FTS5, accents and case ignored); files rank by their best chunk (BM25, headings
  count double), and each hit carries a snippet of that chunk.
- **Both** (`search_all`): the two rankings merged by reciprocal rank fusion.

Filters (all modes): library, folder, kind, size and modification time.

What can be found depends on the scope, and is decided **in the SQL query**:
- `normal`: never anything in a locked folder, a hidden folder (`visible: false`), a
  folder holding copies of other files (trash, edit backups, `trash`-type folders), or
  steward-ai's own `.steward` folder.
- `locked`: only files in locked folders (the Locked Folder view, after a re-auth), and
  never anything those other rules hide (a locked folder's trash stays out of it).
"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from steward.core.db import Databases
from steward.core.folders.paths import FolderRef, PathError
from steward.core.folders.policy import COPY_HOLDING_ROLES, Policy
from steward.core.folders.store import STEWARD_DIR
from steward.sdk import MATCH_MARKERS

type Scope = Literal["normal", "locked"]
type Mode = Literal["all", "names", "text"]
type Snippet = tuple[tuple[str, bool], ...]
"""Text segments and whether each one matched the query."""

MAX_TOKENS = 10
MAX_TOKEN_LENGTH = 200
MAX_LIMIT = 200
COUNT_CAP = 10_000
RANK_CAP = 1000
"""How deep `search_all` looks into each ranking (pages beyond this aren't offered)."""
RRF_K = 60
SNIPPET_TOKENS = 24
START, END = MATCH_MARKERS


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
    snippet: Snippet = ()
    """From the best matching chunk, for text matches."""
    page: int | None = None
    """The page of the best matching chunk, if the document has pages."""

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
            "snippet": "".join(text for text, _ in self.snippet) or None,
            "page": self.page,
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
    concealed, locked = _folder_sets(policy)
    return _outermost(concealed | locked), _outermost(locked)


def _folder_sets(policy: Policy) -> tuple[set[FolderRef], set[FolderRef]]:
    """(folders hidden for reasons other than a lock, folders whose own lock applies)."""
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
    return hidden, locked


class Visibility:
    """Which paths normal views and the Locked Folder view may show (one policy
    snapshot)."""

    def __init__(self, policy: Policy) -> None:
        concealed, locked = _folder_sets(policy)
        self.hidden = _outermost(concealed | locked)
        self.locked = _outermost(locked)
        self.concealed = _outermost(concealed)
        """Hidden for reasons other than a lock (`visible: false`, trash, copies,
        `.steward`): also out of the Locked Folder view."""

    def is_hidden(self, library_id: str, relpath: str) -> bool:
        """Inside a locked, hidden or copy-holding folder."""
        return any(_inside(ref, library_id, relpath) for ref in self.hidden)

    def is_locked(self, library_id: str, relpath: str) -> bool:
        return any(_inside(ref, library_id, relpath) for ref in self.locked)

    def in_locked_view(self, library_id: str, relpath: str) -> bool:
        """Inside a locked folder and not hidden for any other reason."""
        return self.is_locked(library_id, relpath) and not any(
            _inside(ref, library_id, relpath) for ref in self.concealed
        )

    def locked_roots(self) -> list[FolderRef]:
        """The outermost locked folders the Locked Folder view starts from."""
        return [ref for ref in self.locked if self.in_locked_view(ref.library_id, ref.relpath)]

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


def _conditions(
    policy: Policy, filters: SearchFilters, scope: Scope
) -> tuple[list[str], list[Any]] | None:
    """SQL conditions on `files f` (and `idx.blobs b`) for the scope rules and filters.
    None: nothing can match (the locked scope without locked folders)."""
    where = ["f.present = 1"]
    args: list[Any] = []
    visibility = Visibility(policy)
    if scope == "normal":
        for ref in visibility.hidden:
            clause, values = _under(ref)
            where.append(f"NOT {clause}")
            args.extend(values)
    else:
        if not visibility.locked:
            return None
        parts = [_under(ref) for ref in visibility.locked]
        where.append("(" + " OR ".join(c for c, _ in parts) + ")")
        for _, values in parts:
            args.extend(values)
        for ref in visibility.concealed:
            clause, values = _under(ref)
            where.append(f"NOT {clause}")
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
    return where, args


def _check_limits(limit: int, offset: int) -> None:
    if not 1 <= limit <= MAX_LIMIT or offset < 0:
        raise SearchError(f"limit must be 1-{MAX_LIMIT} and offset at least 0")


FILE_COLUMNS = "f.file_id, f.library_id, f.relpath, f.size, f.mtime_ns, b.kind, b.mime"


def _hit(r: Sequence[Any], snippet: Snippet = (), page: int | None = None) -> SearchHit:
    return SearchHit(
        file_id=r[0],
        library_id=r[1],
        relpath=r[2],
        size=r[3],
        modified=datetime.fromtimestamp(r[4] / 1e9, tz=UTC),
        kind=r[5],
        mime=r[6],
        snippet=snippet,
        page=page,
    )


def _count(conn: sqlite3.Connection, sql_from: str, args: Sequence[Any]) -> int:
    return int(
        conn.execute(
            f"SELECT count(*) FROM (SELECT 1 {sql_from} LIMIT {COUNT_CAP + 1})",  # noqa: S608 -- only placeholders are interpolated
            args,
        ).fetchone()[0]
    )


# --- names ---------------------------------------------------------------------------


def _name_query(
    words: list[str], where: list[str], args: list[Any]
) -> tuple[str, list[Any], str, list[Any]]:
    """(FROM clause, its args, ORDER BY clause, its args) for a name search."""
    where = [*where, *("instr(steward_fold(f.relpath), ?) > 0" for _ in words)]
    sql_from = (
        "FROM files f LEFT JOIN idx.blobs b ON b.content_hash = f.content_hash "
        f"WHERE {' AND '.join(where)}"
    )
    name_score = " + ".join("(instr(steward_name(f.relpath), ?) > 0)" for _ in words)
    order = f"({name_score}) DESC, " if words else ""
    return sql_from, [*args, *words], f"{order}f.mtime_ns DESC, f.relpath", list(words)


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
    """Files whose path contains every word of `query` (no words: every file the filters
    allow, newest first)."""
    words = tokens(query)
    _check_limits(limit, offset)
    conditions = _conditions(policy, filters, scope)
    if conditions is None:
        return SearchResults([], 0, capped=False)
    sql_from, args, order, order_args = _name_query(words, *conditions)
    with _connect(dbs) as conn:
        total = _count(conn, sql_from, args)
        rows = conn.execute(
            f"SELECT {FILE_COLUMNS} {sql_from} ORDER BY {order} LIMIT ? OFFSET ?",
            [*args, *order_args, limit, offset],
        ).fetchall()
    return SearchResults([_hit(r) for r in rows], min(total, COUNT_CAP), capped=total > COUNT_CAP)


# --- text ----------------------------------------------------------------------------


def fts_query(words: Sequence[str]) -> str | None:
    """An FTS5 query requiring every word (each quoted, so nothing in it is syntax).
    None if no word has a letter or digit."""
    quoted = ['"' + w.replace('"', '""') + '"' for w in words if any(ch.isalnum() for ch in w)]
    return " ".join(quoted) or None


_MATCHES = (
    "SELECT c.content_hash AS h, min(t.rank) AS score, c.chunk_id AS chunk_id, c.page AS page "
    "FROM idx.chunks_fts t JOIN idx.chunks c ON c.chunk_id = t.rowid "
    "WHERE t.chunks_fts MATCH ? GROUP BY c.content_hash"
)
"""Best chunk per content (SQLite takes bare columns from the row with the minimum)."""


def _text_query(match: str, where: list[str], args: list[Any]) -> tuple[str, list[Any]]:
    sql_from = (
        f"FROM ({_MATCHES}) m JOIN files f ON f.content_hash = m.h "
        "LEFT JOIN idx.blobs b ON b.content_hash = f.content_hash "
        f"WHERE {' AND '.join(where)}"
    )
    return sql_from, [match, *args]


def _snippets(conn: sqlite3.Connection, match: str, chunk_ids: Sequence[int]) -> dict[int, Snippet]:
    if not chunk_ids:
        return {}
    marks = ", ".join("?" for _ in chunk_ids)
    rows = conn.execute(
        "SELECT t.rowid, snippet(chunks_fts, 1, ?, ?, '…', ?) FROM idx.chunks_fts t "  # noqa: S608 -- only placeholders are interpolated
        f"WHERE t.chunks_fts MATCH ? AND t.rowid IN ({marks})",
        [START, END, SNIPPET_TOKENS, match, *chunk_ids],
    ).fetchall()
    return {int(r[0]): split_snippet(r[1]) for r in rows}


def split_snippet(marked: str) -> Snippet:
    """`a \ue000b\ue001 c` -> (("a ", False), ("b", True), (" c", False))."""
    segments: list[tuple[str, bool]] = []
    rest = " ".join(marked.split())
    while rest:
        before, found, after = rest.partition(START)
        if before:
            segments.append((before, False))
        if not found:
            break
        inside, _, rest = after.partition(END)
        if inside:
            segments.append((inside, True))
    return tuple(segments)


def search_text(
    dbs: Databases,
    policy: Policy,
    query: str,
    *,
    filters: SearchFilters = SearchFilters(),  # noqa: B008 -- frozen dataclass
    scope: Scope = "normal",
    limit: int = 50,
    offset: int = 0,
) -> SearchResults:
    """Files whose extracted text has a chunk containing every word of `query`."""
    match = fts_query(tokens(query))
    _check_limits(limit, offset)
    conditions = _conditions(policy, filters, scope)
    if match is None or conditions is None:
        return SearchResults([], 0, capped=False)
    sql_from, args = _text_query(match, *conditions)
    with _connect(dbs) as conn:
        total = _count(conn, sql_from, args)
        rows = conn.execute(
            f"SELECT {FILE_COLUMNS}, m.chunk_id, m.page {sql_from} "
            "ORDER BY m.score, f.mtime_ns DESC, f.relpath LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
        snippets = _snippets(conn, match, [r[7] for r in rows])
    hits = [_hit(r, snippets.get(r[7], ()), r[8]) for r in rows]
    return SearchResults(hits, min(total, COUNT_CAP), capped=total > COUNT_CAP)


# --- both ----------------------------------------------------------------------------


def search_all(
    dbs: Databases,
    policy: Policy,
    query: str,
    *,
    filters: SearchFilters = SearchFilters(),  # noqa: B008 -- frozen dataclass
    scope: Scope = "normal",
    limit: int = 50,
    offset: int = 0,
    mode: Mode = "all",
) -> SearchResults:
    """Names and text, merged by reciprocal rank fusion (each file once; text hits keep
    their snippet). `mode` picks one side instead. Without searchable words this is a
    name search (every file the filters allow)."""
    if mode == "names":
        return search_files(
            dbs, policy, query, filters=filters, scope=scope, limit=limit, offset=offset
        )
    if mode == "text":
        return search_text(
            dbs, policy, query, filters=filters, scope=scope, limit=limit, offset=offset
        )
    words = tokens(query)
    match = fts_query(words)
    if match is None:
        return search_files(
            dbs, policy, query, filters=filters, scope=scope, limit=limit, offset=offset
        )
    _check_limits(limit, offset)
    conditions = _conditions(policy, filters, scope)
    if conditions is None or offset >= RANK_CAP:
        return SearchResults([], 0, capped=False)
    depth = min(offset + limit, RANK_CAP)
    where, args = conditions
    name_from, name_args, name_order, order_args = _name_query(words, where, args)
    text_from, text_args = _text_query(match, where, args)
    with _connect(dbs) as conn:
        by_name = conn.execute(
            f"SELECT {FILE_COLUMNS} {name_from} ORDER BY {name_order} LIMIT ?",
            [*name_args, *order_args, depth],
        ).fetchall()
        by_text = conn.execute(
            f"SELECT {FILE_COLUMNS}, m.chunk_id, m.page {text_from} "
            "ORDER BY m.score, f.mtime_ns DESC, f.relpath LIMIT ?",
            [*text_args, depth],
        ).fetchall()
        name_cond = " AND ".join("instr(steward_fold(f.relpath), ?) > 0" for _ in words)
        either = (
            "FROM files f LEFT JOIN idx.blobs b ON b.content_hash = f.content_hash "  # noqa: S608 -- only placeholders are interpolated
            f"WHERE {' AND '.join(where)} AND (({name_cond}) OR f.content_hash IN "
            f"(SELECT h FROM ({_MATCHES})))"
        )
        total = _count(conn, either, [*args, *words, match])
        scores: dict[str, float] = {}
        rows: dict[str, Sequence[Any]] = {}
        for rank, r in enumerate(by_name):
            scores[r[0]] = scores.get(r[0], 0.0) + 1 / (RRF_K + rank + 1)
            rows[r[0]] = r
        text_rows = {r[0]: r for r in by_text}
        for rank, r in enumerate(by_text):
            scores[r[0]] = scores.get(r[0], 0.0) + 1 / (RRF_K + rank + 1)
            rows.setdefault(r[0], r)
        ordered = sorted(scores, key=lambda fid: (-scores[fid], -rows[fid][4], rows[fid][2]))
        page = ordered[offset : offset + limit]
        snippets = _snippets(conn, match, [text_rows[f][7] for f in page if f in text_rows])
    hits = []
    for fid in page:
        t = text_rows.get(fid)
        hits.append(_hit(rows[fid], snippets.get(t[7], ()) if t else (), t[8] if t else None))
    capped = total > COUNT_CAP or total > RANK_CAP
    return SearchResults(hits, min(total, COUNT_CAP, RANK_CAP), capped=capped)


def _ns(value: datetime) -> int:
    if value.tzinfo is None:
        raise SearchError("dates must include a timezone")
    return int(value.timestamp() * 1e9)
