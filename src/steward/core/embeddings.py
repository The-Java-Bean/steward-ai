# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Embedding spaces and vectors in `index.db` (docs/adr/0023).

A space holds the vectors one model made for one role (`model_key`: model, size and
document prefix); vectors of different spaces are never compared. Vectors are stored
L2-normalized as float32 BLOBs in a plain table that cascades from `chunks`; search
compares them with sqlite-vec's `vec_distance_cosine` (`core.search`).

The space searched is the one of the role's current model (partial while it fills).
When it's complete (`core.stages.jobs.complete_spaces`), the role's other spaces are
deleted.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import struct
import threading
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Connection, delete, func, select
from sqlalchemy.dialects.sqlite import insert

from steward.core.config import RoleConfig
from steward.core.db import Databases, utcnow
from steward.core.db.schema import chunks, embedding_spaces, embeddings
from steward.core.llm.transport import EmbedRequest

if TYPE_CHECKING:
    from steward.core.llm import Gateway
    from steward.core.llm.roles import RoleState

log = logging.getLogger(__name__)

QUERY_TIMEOUT_S = 15.0
QUERY_CACHE = 128
MAX_DIMS = 16_384


class EmbeddingError(ValueError):
    """Vectors that can't be stored (wrong count or size, not finite, all zero)."""


@dataclass(frozen=True, slots=True)
class Space:
    space_id: int
    role: str
    endpoint: str
    model: str
    dims: int
    model_key: str
    status: str
    created_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class VectorQuery:
    """A query vector for `core.search` (made by `QueryEmbedder`)."""

    space_id: int
    vector: bytes
    min_similarity: float
    depth: int = 50


def pack(vector: Sequence[float]) -> bytes:
    """L2-normalized float32 bytes."""
    if not 0 < len(vector) <= MAX_DIMS:
        raise EmbeddingError(f"a vector must have 1-{MAX_DIMS} numbers")
    values = [float(v) for v in vector]
    if not all(math.isfinite(v) for v in values):
        raise EmbeddingError("a vector holds a number that isn't finite")
    norm = math.sqrt(math.fsum(v * v for v in values))
    if norm == 0:
        raise EmbeddingError("a vector is all zeros")
    return struct.pack(f"<{len(values)}f", *(v / norm for v in values))


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _space(row: object) -> Space:
    m = row._mapping  # type: ignore[attr-defined]
    return Space(**{k: m[k] for k in Space.__dataclass_fields__})


def spaces(conn: Connection, role: str | None = None) -> list[Space]:
    query = select(embedding_spaces).order_by(embedding_spaces.c.space_id)
    if role is not None:
        query = query.where(embedding_spaces.c.role == role)
    return [_space(r) for r in conn.execute(query)]


def find_space(conn: Connection, role: str, model_key: str) -> Space | None:
    row = conn.execute(
        select(embedding_spaces).where(
            embedding_spaces.c.role == role, embedding_spaces.c.model_key == model_key
        )
    ).one_or_none()
    return _space(row) if row is not None else None


def ensure_space(
    conn: Connection, *, role: str, mapping: RoleConfig, model_key: str, dims: int
) -> Space:
    """The space for `role`'s model, created on first use. Call in `index.write()`."""
    found = find_space(conn, role, model_key)
    if found is not None:
        if found.dims != dims:
            raise EmbeddingError(f"the model returned {dims} numbers per vector, not {found.dims}")
        return found
    conn.execute(
        insert(embedding_spaces)
        .values(
            role=role,
            endpoint=mapping.endpoint,
            model=mapping.model,
            dims=dims,
            model_key=model_key,
            status="filling",
            created_at=utcnow(),
        )
        .on_conflict_do_nothing()
    )
    created = find_space(conn, role, model_key)
    if created is None:  # unreachable: inserted above in this transaction
        raise EmbeddingError("the embedding space couldn't be created")
    log.info(
        "embedding space created",
        extra={"fields": {"role": role, "model": mapping.model, "dims": dims}},
    )
    return created


def write(
    conn: Connection,
    content_hash: str,
    *,
    space: Space,
    source: str,
    vectors: Sequence[Sequence[float]],
) -> None:
    """Replace this content's vectors in `space`: one per chunk of `source`, in order."""
    chunk_ids = list(
        conn.execute(
            select(chunks.c.chunk_id)
            .where(chunks.c.content_hash == content_hash, chunks.c.artifact == source)
            .order_by(chunks.c.seq)
        ).scalars()
    )
    if len(vectors) != len(chunk_ids):
        raise EmbeddingError(
            f"{len(vectors)} vectors for {len(chunk_ids)} chunks of {source!r} (chunks changed?)"
        )
    if any(len(v) != space.dims for v in vectors):
        raise EmbeddingError(f"every vector must have {space.dims} numbers")
    owned = select(chunks.c.chunk_id).where(chunks.c.content_hash == content_hash)
    conn.execute(
        delete(embeddings).where(
            embeddings.c.space_id == space.space_id, embeddings.c.chunk_id.in_(owned)
        )
    )
    if chunk_ids:
        conn.execute(
            insert(embeddings),
            [
                {"space_id": space.space_id, "chunk_id": cid, "vector": pack(v)}
                for cid, v in zip(chunk_ids, vectors, strict=True)
            ],
        )


def count(conn: Connection, space_id: int) -> int:
    return int(
        conn.execute(
            select(func.count()).select_from(embeddings).where(embeddings.c.space_id == space_id)
        ).scalar_one()
    )


def load_vec(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec into a raw connection. False if this Python can't load
    extensions (semantic search is then off; keyword search still works)."""
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
    except (AttributeError, ImportError, sqlite3.OperationalError):
        log.warning("sqlite-vec can't be loaded; searching by meaning is off", exc_info=True)
        return False
    return True


class QueryEmbedder:
    """Turns a search query into a `VectorQuery` for the embedding role's current space.
    One per process (`Runtime.query_embedder`)."""

    def __init__(self, dbs: Databases, gateway: Gateway, roles: dict[str, RoleState]) -> None:
        self.dbs = dbs
        self.gateway = gateway
        self.roles = sorted(
            (r for r in roles.values() if r.kind == "embed" and r.usable and r.model_key),
            key=lambda r: r.name,
        )
        self._cache: OrderedDict[tuple[int, str], bytes] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.roles)

    def space(self) -> tuple[RoleState, Space] | None:
        """The first embedding role with vectors for its current model."""
        with self.dbs.index.read() as conn:
            for role in self.roles:
                found = find_space(conn, role.name, role.model_key or "")
                if found is not None:
                    return role, found
        return None

    def __call__(self, query: str) -> VectorQuery | None:
        """None: nothing to search by meaning. Raises `ModelUnavailable`."""
        query = " ".join(query.split())
        if not query or not self.roles:
            return None
        found = self.space()
        if found is None:
            return None
        role, space = found
        mapping = self.gateway.role_config(role.name)
        key = (space.space_id, query)
        with self._lock:
            blob = self._cache.get(key)
            if blob is not None:
                self._cache.move_to_end(key)
        if blob is None:
            result = self.gateway.call(
                role.name,
                EmbedRequest([mapping.query_prefix + query], timeout_s=QUERY_TIMEOUT_S),
                provenance=[],
                purpose="index",
            )
            vectors = result.response.vectors
            if len(vectors) != 1 or len(vectors[0]) != space.dims:
                return None
            blob = pack(vectors[0])
            with self._lock:
                self._cache[key] = blob
                while len(self._cache) > QUERY_CACHE:
                    self._cache.popitem(last=False)
        return VectorQuery(space.space_id, blob, mapping.min_similarity)
