# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""A file's effective tags: the tags you added, plus its folder type's `default_tags`
(`rule`) and the AI's choices for its content (`ai`), minus the tags you removed.
Search filters and counts do the same in SQL (`core.search`)."""

from __future__ import annotations

from collections.abc import Iterable
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
    rows = store.all_tags(state)
    by_id = {t.tag_id: t for t in rows}
    canon = store.canonical(rows)
    decisions = store.file_rows(state, file_id)
    sources: dict[int, TagSource] = {}
    if content_hash is not None:
        for tag_id in store.suggestions(index, content_hash):
            if tag_id in canon:
                sources[canon[tag_id]] = "ai"
    for tag_id in resolve_names(rows, rule_names(policy, library_id, relpath)):
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
