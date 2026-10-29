# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Search: file names and paths, and the content index (extracted text).

- **Names** (`search_files`): every word of the query must appear in the file's path
  (case-insensitive); files whose *name* contains the words rank first, then newer files.
- **Text** (`search_text`): every word must appear in one chunk of the file's extracted
  text (FTS5, accents and case ignored); files rank by their best chunk (BM25, headings
  count double), and each hit carries a snippet of that chunk.
- **Meaning** (in `search_all`, when an embedding model is set up): the files whose
  best chunk is closest to the query's embedding (cosine, above the role's
  `min_similarity`, top 50), compared with sqlite-vec's `vec_distance_cosine`.
- **All** (`search_all`): the rankings merged by reciprocal rank fusion. `text` mode
  merges words and meaning; `all` adds names.

Filters (all modes): library, folder, kind, size, modification time, tags (a tag also
finds the tags under it) and properties (EXIF, extracted fields). The query itself may
carry filters (`parse_query`): `tag:finance kind:image amount>100 taken:2024 rent`.

What can be found depends on the scope, and is decided **in the SQL query**:
- `normal`: never anything in a locked folder, a hidden folder (`visible: false`), a
  folder holding copies of other files (trash, edit backups, `trash`-type folders), or
  steward-ai's own `.steward` folder.
- `locked`: only files in locked folders (the Locked Folder view, after a re-auth), and
  never anything those other rules hide (a locked folder's trash stays out of it).
"""

from __future__ import annotations

import dataclasses
import logging
import math
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from steward.core.db import Databases
from steward.core.embeddings import VectorQuery, load_vec
from steward.core.folders.paths import FolderRef, PathError
from steward.core.folders.policy import COPY_HOLDING_ROLES, Policy
from steward.core.folders.store import STEWARD_DIR
from steward.core.tags.names import TagError, is_within, normalize
from steward.core.tags.names import key as tag_key
from steward.sdk import MATCH_MARKERS

type Scope = Literal["normal", "locked"]
type Mode = Literal["all", "names", "text"]
type Snippet = tuple[tuple[str, bool], ...]
"""Text segments and whether each one matched the query."""
type Embed = Callable[[str], VectorQuery | None]
"""Turns a query into a vector (`core.embeddings.QueryEmbedder`); may raise."""

log = logging.getLogger(__name__)

MAX_TOKENS = 10
MAX_TOKEN_LENGTH = 200
MAX_LIMIT = 200
COUNT_CAP = 10_000
RANK_CAP = 1000
"""How deep `search_all` looks into each ranking (pages beyond this aren't offered)."""
RRF_K = 60
SNIPPET_TOKENS = 24
PREVIEW_CHARS = 240
"""Length of the chunk start shown for files found only by meaning."""
START, END = MATCH_MARKERS


class SearchError(ValueError):
    pass


type PropertyOp = Literal[":", "=", ">", ">=", "<", "<="]
PROPERTY_OPS: tuple[PropertyOp, ...] = (">=", "<=", ":", "=", ">", "<")


@dataclass(frozen=True, slots=True)
class PropertyFilter:
    """A condition on a property of the file's content (any artifact: `exif`, `fields`…).
    `:` means "contains" for text and "equals" for numbers; the comparisons compare
    numbers as numbers and anything else as text (so ISO dates compare correctly)."""

    key: str
    op: PropertyOp
    value: str


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
    tags: tuple[str, ...] = ()
    """Tag names; every one must match (a tag or one under it)."""
    properties: tuple[PropertyFilter, ...] = ()


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
    semantic: bool = False
    """Found only by meaning (no word matched its name or text)."""

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
            "semantic": self.semantic,
        }


@dataclass(frozen=True, slots=True)
class SearchResults:
    hits: list[SearchHit]
    total: int
    """Number of matches, counted up to `COUNT_CAP`."""
    capped: bool
    semantic: bool | None = None
    """Whether searching by meaning was part of it: None when it isn't set up (or the
    mode has no text), False when the model couldn't be reached."""


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
    conn: sqlite3.Connection, policy: Policy, filters: SearchFilters, scope: Scope
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
    if filters.tags:
        vocabulary = _Vocabulary.load(conn)
        for name in filters.tags:
            clause, values = _tag_condition(vocabulary, policy, name)
            where.append(clause)
            args.extend(values)
    for prop in filters.properties:
        clause, values = _property_condition(prop)
        where.append(clause)
        args.extend(values)
    return where, args


# --- tags and properties -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Tag:
    tag_id: int
    key: str
    name: str
    target: int | None


@dataclass(frozen=True, slots=True)
class _Vocabulary:
    tags: dict[str, _Tag]
    """name key -> row (aliases too)."""

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> _Vocabulary:
        rows = conn.execute("SELECT tag_id, name_key, name, target_id FROM tags").fetchall()
        return cls({r[1]: _Tag(int(r[0]), r[1], r[2], r[3]) for r in rows})

    def resolve(self, name: str) -> _Tag | None:
        try:
            found = self.tags.get(tag_key(normalize(name)))
        except TagError:
            return None
        if found is not None and found.target is not None:
            return next((t for t in self.tags.values() if t.tag_id == found.target), None)
        return found

    def canonical(self) -> dict[int, int]:
        return {
            t.tag_id: t.target if t.target is not None else t.tag_id for t in self.tags.values()
        }


_REMOVED = (
    "EXISTS (SELECT 1 FROM file_tags r WHERE r.file_id = f.file_id AND r.state = 'removed' "
    "AND r.tag_id = {tag})"
)
_REMOVED_SUGGESTION = _REMOVED.format(
    tag="coalesce((SELECT t.target_id FROM tags t WHERE t.tag_id = s.tag_id), s.tag_id)"
)


def _rule_folders(policy: Policy) -> list[tuple[FolderRef, tuple[str, ...], list[FolderRef]]]:
    """(folder, its type's default tags, the folders inside it with a type of their own)
    for each library root and each folder mapped to a type that has default tags."""
    typed = [ref for ref, m in policy.mappings.items() if m.type_name is not None]
    candidates = [*typed, *(FolderRef(lib, "") for lib in policy.libraries)]
    found = []
    for ref in candidates:
        if ref.library_id not in policy.libraries:
            continue
        try:
            default_tags = policy.settings_for(ref).type.default_tags
        except PathError:
            continue
        if not default_tags:
            continue
        inside = [
            d
            for d in typed
            if d != ref
            and d.library_id == ref.library_id
            and (ref.relpath == "" or d.relpath.startswith(ref.relpath + "/"))
        ]
        nearest = [
            d
            for d in inside
            if not any(o != d and d.relpath.startswith(o.relpath + "/") for o in inside)
        ]
        found.append((ref, default_tags, nearest))
    return found


def _tag_condition(vocabulary: _Vocabulary, policy: Policy, name: str) -> tuple[str, list[Any]]:
    """Files having the tag `name` or one under it: added by you, chosen by the AI for
    the content or given by the folder's type, and not removed by you."""
    tag = vocabulary.resolve(name)
    try:  # a parent that isn't a tag itself (`finance` for `finance/receipt`) works too
        root = tag.key if tag is not None else tag_key(normalize(name))
    except TagError:
        return "0", []
    members = {
        t.tag_id for t in vocabulary.tags.values() if t.target is None and is_within(t.key, root)
    }
    if not members:
        return "0", []
    canon = vocabulary.canonical()
    ids = sorted(i for i, c in canon.items() if c in members)
    member_marks = ", ".join("?" for _ in members)
    id_marks = ", ".join("?" for _ in ids)
    added = (
        "EXISTS (SELECT 1 FROM file_tags u WHERE u.file_id = f.file_id "  # noqa: S608 -- only placeholders are interpolated
        f"AND u.state = 'added' AND u.tag_id IN ({member_marks}))"
    )
    suggested = (
        "EXISTS (SELECT 1 FROM idx.tag_suggestions s WHERE s.content_hash = f.content_hash "  # noqa: S608 -- only placeholders are interpolated
        f"AND s.tag_id IN ({id_marks}) AND NOT {_REMOVED_SUGGESTION})"
    )
    parts = [added, suggested]
    args: list[Any] = [*sorted(members), *ids]
    by_key = {t.key: t for t in vocabulary.tags.values()}
    for ref, default_tags, nearest in _rule_folders(policy):
        for rule in default_tags:
            try:
                row = by_key.get(tag_key(normalize(rule)))
            except TagError:
                continue
            if row is None or canon.get(row.tag_id) not in members:
                continue
            clause, values = _under(ref)
            excluded = [_under(d) for d in nearest]
            parts.append(
                "("
                + " AND ".join(
                    [clause, *(f"NOT {c}" for c, _ in excluded), "NOT " + _REMOVED.format(tag="?")]
                )
                + ")"
            )
            args.extend(values)
            for _, v in excluded:
                args.extend(v)
            args.append(canon[row.tag_id])
    return "(" + " OR ".join(parts) + ")", args


def _number(value: str) -> float | None:
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _property_condition(prop: PropertyFilter) -> tuple[str, list[Any]]:
    number = _number(prop.value)
    base = (
        "EXISTS (SELECT 1 FROM idx.properties p "
        "WHERE p.content_hash = f.content_hash AND p.key = ? AND "
    )
    if prop.op == ":":
        if number is not None:
            return base + "(p.num = ? OR instr(steward_fold(p.text), ?) > 0))", [
                prop.key,
                number,
                fold(prop.value),
            ]
        return base + "instr(steward_fold(p.text), ?) > 0)", [prop.key, fold(prop.value)]
    op = "=" if prop.op == "=" else prop.op
    if number is not None:
        return (
            base + f"((p.num IS NOT NULL AND p.num {op} ?) OR (p.num IS NULL AND p.text {op} ?)))",
            [prop.key, number, prop.value],
        )
    if prop.op == "=":
        return base + "steward_fold(p.text) = ?)", [prop.key, fold(prop.value)]
    return base + f"p.text {op} ?)", [prop.key, prop.value]


_FILTER = re.compile(r'(?<!\S)([A-Za-z][A-Za-z0-9_]*)(>=|<=|:|=|>|<)("([^"]*)"|[^\s"]+)(?!\S)')
_KEYS = (
    "WITH RECURSIVE k(key) AS (SELECT min(key) FROM idx.properties UNION ALL "
    "SELECT (SELECT min(key) FROM idx.properties WHERE key > k.key) FROM k "
    "WHERE k.key IS NOT NULL) SELECT key FROM k WHERE key IS NOT NULL"
)
"""Every property name, one index lookup each."""


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    words: str
    """The query without the filters."""
    tags: tuple[str, ...] = ()
    kind: str | None = None
    properties: tuple[PropertyFilter, ...] = ()


def parse_query(query: str, keys: frozenset[str] | set[str]) -> ParsedQuery:
    """Take filters out of a query: `tag:NAME`, `kind:KIND` and, for property names in
    `keys`, `KEY:VALUE`, `KEY=VALUE`, `KEY>VALUE`… (values may be "double quoted").
    Anything else stays a word."""
    tags: list[str] = []
    props: list[PropertyFilter] = []
    kind: str | None = None
    kept: list[str] = []
    last = 0
    for m in _FILTER.finditer(query):
        name, op = m.group(1).lower(), m.group(2)
        value = m.group(4) if m.group(4) is not None else m.group(3)
        if name == "tag" and op == ":":
            tags.append(value)
        elif name == "kind" and op == ":":
            kind = value.lower()
        elif name in keys:
            props.append(PropertyFilter(name, op, value))  # type: ignore[arg-type]
        else:
            continue
        kept.append(query[last : m.start()])
        last = m.end()
    kept.append(query[last:])
    return ParsedQuery(" ".join("".join(kept).split()), tuple(tags), kind, tuple(props))


def property_keys(dbs: Databases) -> frozenset[str]:
    with _connect(dbs) as conn:
        return frozenset(r[0] for r in conn.execute(_KEYS))


def with_query_filters(
    dbs: Databases, query: str, filters: SearchFilters
) -> tuple[str, SearchFilters]:
    """`query` without its filters, and `filters` with them added."""
    if not _FILTER.search(query):
        return query, filters
    parsed = parse_query(query, property_keys(dbs))
    return parsed.words, dataclasses.replace(
        filters,
        tags=(*filters.tags, *parsed.tags),
        kind=parsed.kind or filters.kind,
        properties=(*filters.properties, *parsed.properties),
    )


def tag_counts(dbs: Databases, policy: Policy, *, scope: Scope = "normal") -> dict[str, int]:
    """Tag name -> how many files in `scope` have it (exactly that tag)."""
    with _connect(dbs) as conn:
        conditions = _conditions(conn, policy, SearchFilters(), scope)
        if conditions is None:
            return {}
        where, args = conditions
        vocabulary = _Vocabulary.load(conn)
        canon = vocabulary.canonical()
        rules: list[str] = []
        rule_args: list[Any] = []
        by_key = {t.key: t for t in vocabulary.tags.values()}
        for ref, default_tags, nearest in _rule_folders(policy):
            for rule in default_tags:
                try:
                    row = by_key.get(tag_key(normalize(rule)))
                except TagError:
                    continue
                if row is None:
                    continue
                clause, values = _under(ref)
                excluded = [_under(d) for d in nearest]
                inside = " AND ".join([clause, *(f"NOT {c}" for c, _ in excluded)])
                rules.append(f"SELECT f.file_id, ? FROM vis f WHERE {inside}")  # noqa: S608 -- only placeholders are interpolated
                rule_args.extend([canon[row.tag_id], *values])
                for _, v in excluded:
                    rule_args.extend(v)
        union = "".join(f" UNION {r}" for r in rules)
        visible = " AND ".join(where)
        rows = conn.execute(
            "WITH vis AS (SELECT f.file_id, f.content_hash, f.library_id, f.relpath FROM files f "  # noqa: S608 -- only placeholders are interpolated
            f"LEFT JOIN idx.blobs b ON b.content_hash = f.content_hash WHERE {visible}), "
            "eff(file_id, tag_id) AS ("
            "SELECT u.file_id, u.tag_id FROM file_tags u JOIN vis v ON v.file_id = u.file_id "
            "WHERE u.state = 'added' UNION "
            "SELECT v.file_id, coalesce(t.target_id, s.tag_id) FROM vis v "
            "JOIN idx.tag_suggestions s ON s.content_hash = v.content_hash "
            f"JOIN tags t ON t.tag_id = s.tag_id{union}) "
            "SELECT e.tag_id, count(*) FROM eff e WHERE NOT EXISTS (SELECT 1 FROM file_tags r "
            "WHERE r.file_id = e.file_id AND r.tag_id = e.tag_id AND r.state = 'removed') "
            "GROUP BY e.tag_id",
            [*args, *rule_args],
        ).fetchall()
    names = {t.tag_id: t.name for t in vocabulary.tags.values() if t.target is None}
    return {names[r[0]]: int(r[1]) for r in rows if r[0] in names}


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
    query, filters = with_query_filters(dbs, query, filters)
    words = tokens(query)
    _check_limits(limit, offset)
    with _connect(dbs) as conn:
        conditions = _conditions(conn, policy, filters, scope)
        if conditions is None:
            return SearchResults([], 0, capped=False)
        sql_from, args, order, order_args = _name_query(words, *conditions)
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
    query, filters = with_query_filters(dbs, query, filters)
    match = fts_query(tokens(query))
    _check_limits(limit, offset)
    if match is None:
        return SearchResults([], 0, capped=False)
    with _connect(dbs) as conn:
        conditions = _conditions(conn, policy, filters, scope)
        if conditions is None:
            return SearchResults([], 0, capped=False)
        sql_from, args = _text_query(match, *conditions)
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


_NEAREST = (
    "SELECT c.content_hash AS h, min(vec_distance_cosine(e.vector, ?)) AS dist, "
    "c.chunk_id AS chunk_id, c.page AS page "
    "FROM idx.embeddings e JOIN idx.chunks c ON c.chunk_id = e.chunk_id "
    "WHERE e.space_id = ? GROUP BY c.content_hash"
)
"""Closest chunk per content in one embedding space."""


def _query_vector(embed: Embed | None, query: str) -> tuple[VectorQuery | None, bool | None]:
    """(vector, semantic flag). Never raises: search works without the model."""
    if embed is None:
        return None, None
    try:
        vector = embed(query)
    except Exception as exc:
        log.warning(
            "query embedding failed; searching by words only",
            extra={"fields": {"error": type(exc).__name__}},
        )
        return None, False
    return vector, (True if vector is not None else None)


def _previews(conn: sqlite3.Connection, chunk_ids: Sequence[int]) -> dict[int, Snippet]:
    if not chunk_ids:
        return {}
    marks = ", ".join("?" for _ in chunk_ids)
    rows = conn.execute(
        f"SELECT chunk_id, substr(body, 1, ?) FROM idx.chunks WHERE chunk_id IN ({marks})",  # noqa: S608 -- only placeholders are interpolated
        [PREVIEW_CHARS + 1, *chunk_ids],
    ).fetchall()
    found: dict[int, Snippet] = {}
    for chunk_id, body in rows:
        text = " ".join(str(body).split())
        if len(text) > PREVIEW_CHARS:
            text = text[:PREVIEW_CHARS].rsplit(" ", 1)[0] + " …"
        found[int(chunk_id)] = ((text, False),) if text else ()
    return found


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
    embed: Embed | None = None,
) -> SearchResults:
    """Names, words in the text and (with `embed`) meaning, merged by reciprocal rank
    fusion: each file once, text hits keep their snippet. `mode` `names` searches names
    only, `text` words and meaning. Without searchable words this is a name search
    (every file the filters allow)."""
    query, filters = with_query_filters(dbs, query, filters)
    if mode == "names":
        return search_files(
            dbs, policy, query, filters=filters, scope=scope, limit=limit, offset=offset
        )
    words = tokens(query)
    match = fts_query(words)
    if match is None:
        if mode == "text":
            return SearchResults([], 0, capped=False)
        return search_files(
            dbs, policy, query, filters=filters, scope=scope, limit=limit, offset=offset
        )
    _check_limits(limit, offset)
    vector, semantic = _query_vector(embed, query)
    if vector is None and mode == "text":
        found = search_text(
            dbs, policy, query, filters=filters, scope=scope, limit=limit, offset=offset
        )
        return SearchResults(found.hits, found.total, found.capped, semantic)
    if offset >= RANK_CAP:
        return SearchResults([], 0, capped=False, semantic=semantic)
    depth = min(offset + limit, RANK_CAP)
    with _connect(dbs) as conn:
        conditions = _conditions(conn, policy, filters, scope)
        if conditions is None:
            return SearchResults([], 0, capped=False, semantic=semantic)
        where, args = conditions
        rankings: list[list[Sequence[Any]]] = []
        either: list[str] = []
        either_args: list[Any] = []
        by_name: list[Sequence[Any]] = []
        if mode == "all":
            name_from, name_args, name_order, order_args = _name_query(words, where, args)
            by_name = conn.execute(
                f"SELECT {FILE_COLUMNS} {name_from} ORDER BY {name_order} LIMIT ?",
                [*name_args, *order_args, depth],
            ).fetchall()
            rankings.append(by_name)
            either.append(
                "(" + " AND ".join("instr(steward_fold(f.relpath), ?) > 0" for _ in words) + ")"
            )
            either_args.extend(words)
        text_from, text_args = _text_query(match, where, args)
        by_text = conn.execute(
            f"SELECT {FILE_COLUMNS}, m.chunk_id, m.page {text_from} "
            "ORDER BY m.score, f.mtime_ns DESC, f.relpath LIMIT ?",
            [*text_args, depth],
        ).fetchall()
        rankings.append(by_text)
        either.append(f"f.content_hash IN (SELECT h FROM ({_MATCHES}))")  # noqa: S608 -- constant SQL
        either_args.append(match)
        by_vector: list[Sequence[Any]] = []
        if vector is not None:
            if load_vec(conn):
                by_vector = conn.execute(
                    f"SELECT {FILE_COLUMNS}, v.chunk_id, v.page FROM ({_NEAREST}) v "  # noqa: S608 -- only placeholders are interpolated
                    "JOIN files f ON f.content_hash = v.h "
                    "LEFT JOIN idx.blobs b ON b.content_hash = f.content_hash "
                    f"WHERE {' AND '.join(where)} AND v.dist <= ? "
                    "ORDER BY v.dist, f.mtime_ns DESC, f.relpath LIMIT ?",
                    [
                        vector.vector,
                        vector.space_id,
                        *args,
                        1.0 - vector.min_similarity,
                        vector.depth,
                    ],
                ).fetchall()
                rankings.append(by_vector)
                if by_vector:
                    either.append(f"f.file_id IN ({', '.join('?' for _ in by_vector)})")
                    either_args.extend(r[0] for r in by_vector)
            else:
                semantic = False
        total = _count(
            conn,
            "FROM files f LEFT JOIN idx.blobs b ON b.content_hash = f.content_hash "
            f"WHERE {' AND '.join(where)} AND ({' OR '.join(either)})",
            [*args, *either_args],
        )
        scores: dict[str, float] = {}
        rows: dict[str, Sequence[Any]] = {}
        for ranking in rankings:
            for rank, r in enumerate(ranking):
                scores[r[0]] = scores.get(r[0], 0.0) + 1 / (RRF_K + rank + 1)
                rows.setdefault(r[0], r)
        text_rows = {r[0]: r for r in by_text}
        vector_rows = {r[0]: r for r in by_vector}
        matched = {r[0] for r in by_name} | set(text_rows)
        ordered = sorted(scores, key=lambda fid: (-scores[fid], -rows[fid][4], rows[fid][2]))
        page = ordered[offset : offset + limit]
        snippets = _snippets(conn, match, [text_rows[f][7] for f in page if f in text_rows])
        previews = _previews(
            conn, [vector_rows[f][7] for f in page if f in vector_rows and f not in text_rows]
        )
    hits = []
    for fid in page:
        t = text_rows.get(fid)
        v = vector_rows.get(fid)
        if t is not None:
            hits.append(_hit(rows[fid], snippets.get(t[7], ()), t[8]))
        elif v is not None:
            hit = _hit(rows[fid], previews.get(v[7], ()), v[8])
            hits.append(dataclasses.replace(hit, semantic=fid not in matched))
        else:
            hits.append(_hit(rows[fid]))
    capped = total > COUNT_CAP or total > RANK_CAP
    return SearchResults(hits, min(total, COUNT_CAP, RANK_CAP), capped=capped, semantic=semantic)


def _ns(value: datetime) -> int:
    if value.tzinfo is None:
        raise SearchError("dates must include a timezone")
    return int(value.timestamp() * 1e9)
