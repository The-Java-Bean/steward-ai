# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The content index in `index.db`: extracted text, chunks and properties, keyed by
content hash.

Stages write it through `StageContext.write_text` / `write_chunks` / `write_properties`
(each call replaces what was stored for that content and artifact kind); the core
searches it (`steward.core.search`), so the scope and privacy rules are applied in one
place. `chunks_fts` follows `chunks` through triggers (index revision 0004); properties
came in index revision 0006 (docs/adr/0024).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import Connection, delete, insert, select

from steward.core.db import utcnow
from steward.core.db.schema import chunks, properties, texts
from steward.sdk import MATCH_MARKERS, ChunkPart, PropertyValue, TextPart

PROPERTY_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_PROPERTIES = 200
MAX_PROPERTY_CHARS = 2000

_STRIP = str.maketrans(dict.fromkeys([*MATCH_MARKERS, "\x00"], " "))


def clean(text: str) -> str:
    """Text as stored: no NUL or snippet marker characters, valid UTF-8 (lone
    surrogates from lenient decoding become `?`)."""
    return text.translate(_STRIP).encode("utf-8", "replace").decode("utf-8")


def _clean_opt(text: str | None) -> str | None:
    if text is None:
        return None
    text = clean(text).strip()
    return text or None


def write_text(
    conn: Connection,
    content_hash: str,
    artifact: str,
    parts: Sequence[TextPart],
    *,
    now: datetime | None = None,
) -> None:
    now = now or utcnow()
    conn.execute(
        delete(texts).where(texts.c.content_hash == content_hash, texts.c.artifact == artifact)
    )
    rows = [
        {
            "content_hash": content_hash,
            "artifact": artifact,
            "part": i,
            "page": p.page,
            "heading": _clean_opt(p.heading),
            "body": clean(p.body),
            "created_at": now,
        }
        for i, p in enumerate(parts)
    ]
    if rows:
        conn.execute(insert(texts), rows)


def read_text(conn: Connection, content_hash: str, artifact: str) -> list[TextPart]:
    rows = conn.execute(
        select(texts.c.body, texts.c.page, texts.c.heading)
        .where(texts.c.content_hash == content_hash, texts.c.artifact == artifact)
        .order_by(texts.c.part)
    )
    return [TextPart(r.body, page=r.page, heading=r.heading) for r in rows]


def write_chunks(
    conn: Connection,
    content_hash: str,
    artifact: str,
    source: str,
    parts: Sequence[ChunkPart],
    *,
    now: datetime | None = None,
) -> None:
    now = now or utcnow()
    conn.execute(
        delete(chunks).where(chunks.c.content_hash == content_hash, chunks.c.artifact == artifact)
    )
    rows = [
        {
            "content_hash": content_hash,
            "artifact": artifact,
            "seq": i,
            "source": source,
            "page": p.page,
            "heading": _clean_opt(p.heading),
            "body": clean(p.body),
            "created_at": now,
        }
        for i, p in enumerate(parts)
        if p.body.strip()
    ]
    if rows:
        conn.execute(insert(chunks), rows)


def read_chunks(conn: Connection, content_hash: str, artifact: str) -> list[ChunkPart]:
    rows = conn.execute(
        select(chunks.c.body, chunks.c.page, chunks.c.heading)
        .where(chunks.c.content_hash == content_hash, chunks.c.artifact == artifact)
        .order_by(chunks.c.seq)
    )
    return [ChunkPart(r.body, page=r.page, heading=r.heading) for r in rows]


def text_of(conn: Connection, content_hash: str) -> dict[str, list[TextPart]]:
    """Every text artifact stored for a content (for `steward files text`)."""
    found: dict[str, list[TextPart]] = {}
    rows = conn.execute(
        select(texts.c.artifact, texts.c.body, texts.c.page, texts.c.heading)
        .where(texts.c.content_hash == content_hash)
        .order_by(texts.c.artifact, texts.c.part)
    )
    for r in rows:
        found.setdefault(r.artifact, []).append(TextPart(r.body, page=r.page, heading=r.heading))
    return found


# --- properties ----------------------------------------------------------------------


def _property_row(key: str, value: PropertyValue) -> dict[str, object]:
    if not PROPERTY_KEY.match(key):
        raise ValueError(f"property name {key!r} isn't snake_case (at most 64 characters)")
    if isinstance(value, bool):
        return {"key": key, "type": "bool", "text": None, "num": float(value)}
    if isinstance(value, int):
        if abs(value) > 2**53:
            raise ValueError(f"property {key!r} is too large for a number")
        return {"key": key, "type": "int", "text": None, "num": float(value)}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"property {key!r} isn't a finite number")
        return {"key": key, "type": "float", "text": None, "num": value}
    if isinstance(value, str):
        text = clean(value).strip()
        if len(text) > MAX_PROPERTY_CHARS:
            raise ValueError(f"property {key!r} is longer than {MAX_PROPERTY_CHARS} characters")
        return {"key": key, "type": "text", "text": text, "num": None}
    raise TypeError(f"property {key!r} must be text, a number or a boolean")


def write_properties(
    conn: Connection,
    content_hash: str,
    artifact: str,
    values: Mapping[str, PropertyValue | None],
    *,
    now: datetime | None = None,
) -> None:
    """Replace this content's properties of `artifact` (None values are left out, as are
    empty strings)."""
    rows = [_property_row(k, v) for k, v in values.items() if v is not None]
    rows = [r for r in rows if r["type"] != "text" or r["text"]]
    if len(rows) > MAX_PROPERTIES:
        raise ValueError(f"at most {MAX_PROPERTIES} properties per artifact")
    now = now or utcnow()
    conn.execute(
        delete(properties).where(
            properties.c.content_hash == content_hash, properties.c.artifact == artifact
        )
    )
    if rows:
        conn.execute(
            insert(properties),
            [
                {"content_hash": content_hash, "artifact": artifact, "created_at": now, **r}
                for r in rows
            ],
        )


def _value(type_: str, text: str | None, num: float | None) -> PropertyValue:
    if type_ == "text":
        return text or ""
    if type_ == "bool":
        return bool(num)
    if type_ == "int":
        return int(num or 0)
    return float(num or 0.0)


def read_properties(conn: Connection, content_hash: str, artifact: str) -> dict[str, PropertyValue]:
    rows = conn.execute(
        select(properties.c.key, properties.c.type, properties.c.text, properties.c.num)
        .where(properties.c.content_hash == content_hash, properties.c.artifact == artifact)
        .order_by(properties.c.key)
    )
    return {r.key: _value(r.type, r.text, r.num) for r in rows}


def properties_of(conn: Connection, content_hash: str) -> dict[str, dict[str, PropertyValue]]:
    """Every property artifact stored for a content: artifact -> key -> value."""
    found: dict[str, dict[str, PropertyValue]] = {}
    rows = conn.execute(
        select(
            properties.c.artifact,
            properties.c.key,
            properties.c.type,
            properties.c.text,
            properties.c.num,
        )
        .where(properties.c.content_hash == content_hash)
        .order_by(properties.c.artifact, properties.c.key)
    )
    for r in rows:
        found.setdefault(r.artifact, {})[r.key] = _value(r.type, r.text, r.num)
    return found
