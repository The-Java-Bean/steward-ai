# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""A file's effective tags: the tags you added, plus its folder type's `default_tags`
(`rule`) and the AI's choices for its content (`ai`), minus the tags you removed.
Search filters and counts do the same in SQL (`core.search`)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from sqlalchemy import Connection

from steward.core.folders.paths import FolderRef, PathError
from steward.core.folders.policy import Policy
from steward.core.tags import store
from steward.core.tags.names import TagError, key, normalize
from steward.sdk import FileTag, TagSource, TagStatus


def rule_names(policy: Policy, library_id: str, relpath: str) -> tuple[str, ...]:
    """The `default_tags` of the folder type of the folder holding `relpath`."""
    folder = relpath.rsplit("/", 1)[0] if "/" in relpath else ""
    try:
        return policy.settings_for(FolderRef(library_id, folder)).type.default_tags
    except (PathError, KeyError):
        return ()


def resolve_names(rows: Iterable[store.Tag], names: Iterable[str]) -> list[int]:
    """The ids of the tags `names` stand for (unknown or bad names left out)."""
    by_key = {t.key: t for t in rows}
    found = []
    for name in names:
        try:
            tag = by_key.get(key(normalize(name)))
        except TagError:
            continue
        if tag is not None:
            found.append(tag.target_id if tag.target_id is not None else tag.tag_id)
    return found


@dataclass(frozen=True, slots=True)
class FileRef:
    file_id: str
    content_hash: str | None
    library_id: str
    relpath: str


def _compose(
    by_id: Mapping[int, store.Tag],
    canon: Mapping[int, int],
    rule_ids: Iterable[int],
    suggested: Iterable[int],
    decisions: Mapping[int, store.FileState],
) -> list[FileTag]:
    sources: dict[int, TagSource] = {}
    for tag_id in suggested:
        if tag_id in canon:
            sources[canon[tag_id]] = "ai"
    for tag_id in rule_ids:
        sources[tag_id] = "rule"
    for tag_id, state_ in decisions.items():
        if state_ == "added":
            sources[tag_id] = "user"
        else:
            sources.pop(tag_id, None)
    found = [
        FileTag(by_id[t].name, source, cast(TagStatus, by_id[t].status))
        for t, source in sources.items()
        if t in by_id
    ]
    return sorted(found, key=lambda tag: key(tag.name))


def file_tags(
    state: Connection,
    index: Connection,
    policy: Policy,
    *,
    file_id: str,
    content_hash: str | None,
    library_id: str,
    relpath: str,
) -> list[FileTag]:
    """The file's tags, by name."""
    file = FileRef(file_id, content_hash, library_id, relpath)
    return many(state, index, policy, [file])[file_id]


def many(
    state: Connection, index: Connection, policy: Policy, files: Sequence[FileRef]
) -> dict[str, list[FileTag]]:
    """Tags of many files (`file_id` -> tags), with one query per kind of row."""
    rows = store.all_tags(state)
    by_id = {t.tag_id: t for t in rows}
    canon = store.canonical(rows)
    decisions = store.file_rows_of(state, [f.file_id for f in files])
    suggested = store.suggestions_of(index, [f.content_hash for f in files if f.content_hash])
    rules: dict[tuple[str, str], list[int]] = {}
    found = {}
    for f in files:
        where = (f.library_id, f.relpath.rsplit("/", 1)[0] if "/" in f.relpath else "")
        if where not in rules:
            rules[where] = resolve_names(rows, rule_names(policy, f.library_id, f.relpath))
        rule_ids = rules[where]
        found[f.file_id] = _compose(
            by_id,
            canon,
            rule_ids,
            suggested.get(f.content_hash, ()) if f.content_hash else (),
            decisions.get(f.file_id, {}),
        )
    return found
