# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The content index in `index.db`: extracted text and chunks, keyed by content hash.

Stages write it through `StageContext.write_text` / `write_chunks` (each call replaces
what was stored for that content and artifact kind); the core searches it
(`steward.core.search`), so the scope and privacy rules are applied in one place.
`chunks_fts` follows `chunks` through triggers (index revision 0004).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Connection, delete, insert, select

from steward.core.db import utcnow
from steward.core.db.schema import chunks, texts
from steward.sdk import MATCH_MARKERS, ChunkPart, TextPart

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
