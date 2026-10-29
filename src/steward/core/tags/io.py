# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The vocabulary as YAML (`steward tags export` / `import`, the bootstrap's output):

    tags:
      - name: finance/receipt
        description: Proof of payment for something bought
        aliases: [receipts]

Importing only adds: new tags (`active`), descriptions for tags that have none, and
aliases whose names are free. Nothing is renamed, deprecated or removed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Connection

from steward.core.db import utcnow
from steward.core.tags import store
from steward.core.tags.names import TagError, normalize

MAX_IMPORT = 5000


class TagEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)
    description: str = Field("", max_length=store.MAX_DESCRIPTION)
    aliases: list[str] = Field(default_factory=list, max_length=50)


class TagFile(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tags: list[TagEntry] = Field(default_factory=list, max_length=MAX_IMPORT)


@dataclass(slots=True)
class Imported:
    created: list[str] = field(default_factory=list)
    described: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def parse(text: str) -> TagFile:
    """Raises TagError for a file that isn't a tag list."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TagError(f"not valid YAML: {exc}") from exc
    if isinstance(data, list):
        data = {"tags": data}
    if not isinstance(data, dict):
        raise TagError("expected a mapping with a `tags:` list")
    try:
        return TagFile.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise TagError(f"{where}: {first['msg']}") from exc


def import_tags(
    conn: Connection, tag_file: TagFile, *, actor: str, now: datetime | None = None
) -> Imported:
    """Add what the file has and the vocabulary lacks; entries with problems are
    skipped and reported."""
    now = now or utcnow()
    result = Imported()
    for entry in tag_file.tags:
        try:
            name = normalize(entry.name)
            found = store.resolve(conn, name)
            if found is None:
                found = store.create(
                    conn, name, description=entry.description.strip(), actor=actor, now=now
                )
                result.created.append(found.name)
            elif entry.description.strip() and not found.description:
                store.update_tag(
                    conn, found.tag_id, description=entry.description.strip(), actor=actor, now=now
                )
                result.described.append(found.name)
        except TagError as exc:
            result.problems.append(f"{entry.name}: {exc}")
            continue
        for alias in entry.aliases:
            try:
                if store.find(conn, alias) is None:
                    store.add_alias(conn, found.tag_id, alias, actor=actor, now=now)
                    result.aliases.append(f"{normalize(alias)} → {found.name}")
            except TagError as exc:
                result.problems.append(f"{entry.name}: alias {alias!r}: {exc}")
    return result


def dump(tag_file: TagFile) -> str:
    entries = [e.model_dump(exclude_defaults=True) for e in tag_file.tags]
    return yaml.safe_dump({"tags": entries}, allow_unicode=True, sort_keys=False, width=100)


def export_tags(conn: Connection) -> str:
    """Every tag but deprecated ones, as YAML `import_tags` reads back."""
    return dump(
        TagFile(
            tags=[
                TagEntry(name=t.name, description=t.description, aliases=list(t.aliases))
                for t in store.vocabulary(conn, usable_only=True)
            ]
        )
    )
