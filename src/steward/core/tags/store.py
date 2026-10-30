# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The tag vocabulary and your decisions about files' tags (`state.db`), and the AI's
choices per content (`index.db`). Every function takes a `Connection` in a transaction
of the right database; changes emit events in it.

Invariants: `file_tags` rows never point at an `alias` row (a merge moves them), and
an alias points at a tag that isn't an alias (renames and merges repoint).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from sqlalchemy import Connection, delete, insert, select, update

from steward.core import events
from steward.core.db import utcnow
from steward.core.db.schema import file_tags, tag_suggestions, tags
from steward.core.tags.names import TagError, is_within, key, normalize
from steward.sdk import TagInfo, TagStatus

USABLE: tuple[TagStatus, ...] = ("active", "ai-created")
"""Statuses a stage may choose."""
VOCABULARY_CHANGED = "tag.vocabulary_changed"
"""Emitted when a tag became usable (created, accepted, activated)."""
MAX_DESCRIPTION = 500
BATCH = 500
"""Most ids in one `IN (...)` list."""

FileState = Literal["added", "removed"]


@dataclass(frozen=True, slots=True)
class Tag:
    tag_id: int
    name: str
    status: str
    target_id: int | None
    description: str

    @property
    def key(self) -> str:
        return key(self.name)

    @property
    def is_alias(self) -> bool:
        return self.status == "alias"


def _tag(row: Any) -> Tag:
    return Tag(row.tag_id, row.name, row.status, row.target_id, row.description)


_COLUMNS = (tags.c.tag_id, tags.c.name, tags.c.status, tags.c.target_id, tags.c.description)


def all_tags(conn: Connection) -> list[Tag]:
    """Every row, aliases included, by name."""
    return [_tag(r) for r in conn.execute(select(*_COLUMNS).order_by(tags.c.name_key))]


def get(conn: Connection, tag_id: int) -> Tag | None:
    row = conn.execute(select(*_COLUMNS).where(tags.c.tag_id == tag_id)).one_or_none()
    return _tag(row) if row is not None else None


def find(conn: Connection, name: str) -> Tag | None:
    """The row with this name (an alias row too), or None."""
    row = conn.execute(
        select(*_COLUMNS).where(tags.c.name_key == key(normalize(name)))
    ).one_or_none()
    return _tag(row) if row is not None else None


def resolve(conn: Connection, name: str) -> Tag | None:
    """The tag `name` stands for (an alias's target), or None."""
    found = find(conn, name)
    if found is not None and found.target_id is not None:
        return get(conn, found.target_id)
    return found


def canonical(rows: Iterable[Tag]) -> dict[int, int]:
    """tag_id -> the id of the tag it stands for (itself unless it's an alias)."""
    return {t.tag_id: t.target_id if t.target_id is not None else t.tag_id for t in rows}


def require(conn: Connection, name: str) -> Tag:
    found = resolve(conn, name)
    if found is None:
        raise TagError(f"there's no tag {normalize(name)!r}")
    return found


def vocabulary(conn: Connection, *, usable_only: bool = False) -> list[TagInfo]:
    """Every tag but aliases (with their aliases' names), by name."""
    rows = all_tags(conn)
    aliases: dict[int, list[str]] = {}
    for t in rows:
        if t.target_id is not None:
            aliases.setdefault(t.target_id, []).append(t.name)
    statuses = USABLE if usable_only else (*USABLE, "deprecated")
    return [
        TagInfo(
            name=t.name,
            status=t.status,
            description=t.description,
            aliases=tuple(sorted(aliases.get(t.tag_id, ()), key=key)),
        )
        for t in rows
        if t.status in statuses
    ]


def _check_description(description: str) -> str:
    description = " ".join(description.split())
    if len(description) > MAX_DESCRIPTION:
        raise TagError(f"a description can be at most {MAX_DESCRIPTION} characters")
    return description


def _free(conn: Connection, name: str) -> None:
    taken = find(conn, name)
    if taken is None:
        return
    target = get(conn, taken.target_id) if taken.target_id is not None else None
    what = f"an old name of {target.name!r}" if target is not None else "a tag"
    raise TagError(f"{taken.name!r} is already {what}")


def create(
    conn: Connection,
    name: str,
    *,
    status: TagStatus = "active",
    description: str = "",
    actor: str,
    now: datetime | None = None,
) -> Tag:
    name = normalize(name)
    _free(conn, name)
    now = now or utcnow()
    tag_id = conn.execute(
        insert(tags)
        .values(
            name=name,
            name_key=key(name),
            status=status,
            description=_check_description(description),
            actor=actor,
            created_at=now,
            updated_at=now,
        )
        .returning(tags.c.tag_id)
    ).scalar_one()
    events.emit(conn, "tag.created", {"tag_id": tag_id, "name": name}, actor=actor, now=now)
    if status in USABLE:
        events.emit(conn, VOCABULARY_CHANGED, {"tag_id": tag_id}, actor=actor, now=now)
    return Tag(tag_id, name, status, None, description)


def ensure(
    conn: Connection, names: Iterable[str], *, actor: str, now: datetime | None = None
) -> list[str]:
    """Create the names that aren't tags (or aliases) yet, as `active`. Returns them."""
    created = []
    for name in names:
        if find(conn, name) is None:
            created.append(create(conn, name, actor=actor, now=now).name)
    return created


def add_alias(
    conn: Connection, tag_id: int, name: str, *, actor: str, now: datetime | None = None
) -> None:
    """Make `name` another name for a tag."""
    tag = get(conn, tag_id)
    if tag is None or tag.is_alias:
        raise TagError("no such tag")
    name = normalize(name)
    _free(conn, name)
    now = now or utcnow()
    conn.execute(
        insert(tags).values(
            name=name,
            name_key=key(name),
            status="alias",
            target_id=tag_id,
            actor=actor,
            created_at=now,
            updated_at=now,
        )
    )


def delete_unused(conn: Connection, tag_id: int, *, actor: str) -> None:
    """Remove a tag nothing refers to (undoing its creation). Tags under it stay."""
    used = conn.execute(select(file_tags.c.file_id).where(file_tags.c.tag_id == tag_id).limit(1))
    aliased = conn.execute(select(tags.c.tag_id).where(tags.c.target_id == tag_id).limit(1))
    if used.first() is not None or aliased.first() is not None:
        raise TagError("the tag is in use now")
    conn.execute(delete(tags).where(tags.c.tag_id == tag_id))
    events.emit(conn, "tag.deleted", {"tag_id": tag_id}, actor=actor)


def update_tag(
    conn: Connection,
    tag_id: int,
    *,
    status: TagStatus | None = None,
    description: str | None = None,
    actor: str,
    now: datetime | None = None,
) -> None:
    current = get(conn, tag_id)
    if current is None or current.is_alias:
        raise TagError("no such tag")
    values: dict[str, Any] = {}
    if status is not None and status != current.status:
        values["status"] = status
    if description is not None and _check_description(description) != current.description:
        values["description"] = _check_description(description)
    if not values:
        return
    now = now or utcnow()
    conn.execute(update(tags).where(tags.c.tag_id == tag_id).values(**values, updated_at=now))
    events.emit(conn, "tag.updated", {"tag_id": tag_id, **values}, actor=actor, now=now)
    if "status" in values and status in USABLE and current.status not in USABLE:
        events.emit(conn, VOCABULARY_CHANGED, {"tag_id": tag_id}, actor=actor, now=now)


# --- rename ----------------------------------------------------------------------------


def subtree(conn: Connection, tag: Tag) -> list[Tag]:
    """The tag and the tags under it (no aliases), by name."""
    return [t for t in all_tags(conn) if not t.is_alias and is_within(t.key, tag.key)]


def rename(
    conn: Connection,
    tag_id: int,
    new_name: str,
    *,
    keep_alias: bool = True,
    actor: str,
    now: datetime | None = None,
) -> list[tuple[str, str]]:
    """Rename a tag and the tags under it. With `keep_alias` each old name stays as an
    alias (undoing a rename doesn't keep one). Returns (old, new) names."""
    tag = get(conn, tag_id)
    if tag is None or tag.is_alias:
        raise TagError("no such tag")
    new_name = normalize(new_name)
    if new_name == tag.name:
        raise TagError("that's its name already")
    if is_within(key(new_name), tag.key):
        raise TagError("a tag can't be moved under itself")
    now = now or utcnow()
    moves = [(t, new_name + t.name[len(tag.name) :]) for t in subtree(conn, tag)]
    for t, target in moves:
        taken = find(conn, target)
        if taken is not None and taken.target_id != t.tag_id and taken.tag_id != t.tag_id:
            raise TagError(f"{taken.name!r} already exists")
    for t, target in moves:
        taken = find(conn, target)
        if taken is not None and taken.target_id == t.tag_id:
            conn.execute(delete(tags).where(tags.c.tag_id == taken.tag_id))
        conn.execute(
            update(tags)
            .where(tags.c.tag_id == t.tag_id)
            .values(name=target, name_key=key(target), updated_at=now)
        )
        if keep_alias:
            conn.execute(
                insert(tags).values(
                    name=t.name,
                    name_key=t.key,
                    status="alias",
                    target_id=t.tag_id,
                    actor=actor,
                    created_at=now,
                    updated_at=now,
                )
            )
    events.emit(
        conn,
        "tag.renamed",
        {"tag_id": tag_id, "old": tag.name, "new": new_name},
        actor=actor,
        now=now,
    )
    return [(t.name, target) for t, target in moves]


# --- merge -----------------------------------------------------------------------------


def prepare_merge(conn: Connection, tag_id: int, into_id: int) -> dict[str, Any]:
    """Check that `tag_id` can be merged into `into_id` and work out what changes: the
    files whose decision moves to the other tag (`moved`), the decisions dropped because
    the file has one about the other tag already (`dropped`), the aliases that will
    point at the other tag (`repointed`). The result is what `unmerge` needs too."""
    tag, into = get(conn, tag_id), get(conn, into_id)
    if tag is None or into is None or tag.is_alias or into.is_alias:
        raise TagError("no such tag")
    if tag_id == into_id:
        raise TagError("a tag can't be merged into itself")
    if len(subtree(conn, tag)) > 1:
        raise TagError(f"{tag.name!r} has tags under it; rename or merge those first")
    if is_within(into.key, tag.key):
        raise TagError("a tag can't be merged into a tag under it")
    rows = conn.execute(
        select(file_tags.c.file_id, file_tags.c.state).where(file_tags.c.tag_id == tag_id)
    ).all()
    taken = set(
        conn.execute(
            select(file_tags.c.file_id).where(
                file_tags.c.tag_id == into_id,
                file_tags.c.file_id.in_([r.file_id for r in rows]),
            )
        ).scalars()
    )
    return {
        "tag_id": tag_id,
        "into": into_id,
        "name": tag.name,
        "into_name": into.name,
        "status": tag.status,
        "moved": [r.file_id for r in rows if r.file_id not in taken],
        "dropped": [[r.file_id, r.state] for r in rows if r.file_id in taken],
        "repointed": list(
            conn.execute(select(tags.c.tag_id).where(tags.c.target_id == tag_id)).scalars()
        ),
    }


def merge(
    conn: Connection, merged: dict[str, Any], *, actor: str, now: datetime | None = None
) -> None:
    """Make a tag an alias of another, as `prepare_merge` worked out: files tagged with
    it get the other tag."""
    tag_id, into_id = int(merged["tag_id"]), int(merged["into"])
    tag = get(conn, tag_id)
    if tag is None or tag.is_alias:
        raise TagError("the tag was changed since; try again")
    now = now or utcnow()
    if merged["moved"]:
        conn.execute(
            update(file_tags)
            .where(file_tags.c.tag_id == tag_id, file_tags.c.file_id.in_(merged["moved"]))
            .values(tag_id=into_id)
        )
    conn.execute(delete(file_tags).where(file_tags.c.tag_id == tag_id))
    conn.execute(update(tags).where(tags.c.target_id == tag_id).values(target_id=into_id))
    conn.execute(
        update(tags)
        .where(tags.c.tag_id == tag_id)
        .values(status="alias", target_id=into_id, updated_at=now)
    )
    events.emit(
        conn,
        "tag.merged",
        {
            "tag_id": tag_id,
            "into": into_id,
            "name": merged["name"],
            "into_name": merged["into_name"],
        },
        actor=actor,
        now=now,
    )


def unmerge(
    conn: Connection, merged: dict[str, Any], *, actor: str, now: datetime | None = None
) -> None:
    tag_id, into_id = int(merged["tag_id"]), int(merged["into"])
    tag = get(conn, tag_id)
    if tag is None or tag.target_id != into_id:
        raise TagError("the merge was changed since")
    now = now or utcnow()
    conn.execute(
        update(tags)
        .where(tags.c.tag_id == tag_id)
        .values(status=merged["status"], target_id=None, updated_at=now)
    )
    if merged["repointed"]:
        conn.execute(
            update(tags)
            .where(tags.c.tag_id.in_(merged["repointed"]), tags.c.target_id == into_id)
            .values(target_id=tag_id)
        )
    if merged["moved"]:
        conn.execute(
            update(file_tags)
            .where(file_tags.c.tag_id == into_id, file_tags.c.file_id.in_(merged["moved"]))
            .values(tag_id=tag_id)
        )
    for file_id, state in merged["dropped"]:
        conn.execute(
            insert(file_tags)
            .values(file_id=file_id, tag_id=tag_id, state=state, actor=actor, changed_at=now)
            .prefix_with("OR IGNORE")
        )
    events.emit(conn, "tag.unmerged", {"tag_id": tag_id, "from": into_id}, actor=actor, now=now)
    if merged["status"] in USABLE:
        events.emit(conn, VOCABULARY_CHANGED, {"tag_id": tag_id}, actor=actor, now=now)


# --- files -----------------------------------------------------------------------------


def file_state(conn: Connection, file_id: str, tag_id: int) -> FileState | None:
    return cast(
        FileState | None,
        conn.execute(
            select(file_tags.c.state).where(
                file_tags.c.file_id == file_id, file_tags.c.tag_id == tag_id
            )
        ).scalar_one_or_none(),
    )


def file_rows(conn: Connection, file_id: str) -> dict[int, FileState]:
    """tag_id -> your decision, for one file."""
    return {
        r.tag_id: cast(FileState, r.state)
        for r in conn.execute(
            select(file_tags.c.tag_id, file_tags.c.state).where(file_tags.c.file_id == file_id)
        )
    }


def file_rows_of(conn: Connection, file_ids: Sequence[str]) -> dict[str, dict[int, FileState]]:
    """file_id -> tag_id -> your decision."""
    found: dict[str, dict[int, FileState]] = {}
    for start in range(0, len(file_ids), BATCH):
        for r in conn.execute(
            select(file_tags.c.file_id, file_tags.c.tag_id, file_tags.c.state).where(
                file_tags.c.file_id.in_(file_ids[start : start + BATCH])
            )
        ):
            found.setdefault(r.file_id, {})[r.tag_id] = cast(FileState, r.state)
    return found


def set_file(
    conn: Connection,
    file_id: str,
    tag_id: int,
    state: FileState | None,
    *,
    actor: str,
    now: datetime | None = None,
) -> FileState | None:
    """Record your decision about one tag of one file (None: no decision). Returns the
    one it replaced."""
    tag = get(conn, tag_id)
    if tag is None or tag.is_alias:
        raise TagError("no such tag")
    now = now or utcnow()
    previous = file_state(conn, file_id, tag_id)
    if previous == state:
        return previous
    conn.execute(
        delete(file_tags).where(file_tags.c.file_id == file_id, file_tags.c.tag_id == tag_id)
    )
    if state is not None:
        conn.execute(
            insert(file_tags).values(
                file_id=file_id, tag_id=tag_id, state=state, actor=actor, changed_at=now
            )
        )
    events.emit(
        conn,
        "file.tags_changed",
        {"file_id": file_id, "tag_id": tag_id, "state": state},
        actor=actor,
        now=now,
    )
    return previous


# --- the AI's choices (index.db) ---------------------------------------------------------


def write_suggestions(conn: Connection, content_hash: str, tag_ids: Iterable[int]) -> None:
    now = utcnow()
    conn.execute(delete(tag_suggestions).where(tag_suggestions.c.content_hash == content_hash))
    rows = [
        {"content_hash": content_hash, "tag_id": t, "created_at": now}
        for t in dict.fromkeys(tag_ids)
    ]
    if rows:
        conn.execute(insert(tag_suggestions), rows)


def suggestions(conn: Connection, content_hash: str) -> list[int]:
    return list(
        conn.execute(
            select(tag_suggestions.c.tag_id).where(tag_suggestions.c.content_hash == content_hash)
        ).scalars()
    )


def suggestions_of(conn: Connection, hashes: Sequence[str]) -> dict[str, list[int]]:
    """content hash -> what the AI chose for it."""
    unique = list(dict.fromkeys(hashes))
    found: dict[str, list[int]] = {}
    for start in range(0, len(unique), BATCH):
        for r in conn.execute(
            select(tag_suggestions.c.content_hash, tag_suggestions.c.tag_id).where(
                tag_suggestions.c.content_hash.in_(unique[start : start + BATCH])
            )
        ):
            found.setdefault(r.content_hash, []).append(r.tag_id)
    return found
