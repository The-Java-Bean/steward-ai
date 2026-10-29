# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The `StageContext` a stage's `run` receives: read-only file access (directly or in an
isolated child process), the content index, `index.db` and model roles."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from pydantic import BaseModel
from sqlalchemy import Connection

from steward.core import content, embeddings
from steward.core.db import Databases
from steward.core.llm import ChatRequest, EmbedRequest, Gateway
from steward.core.llm import structured as structured_output
from steward.core.llm.roles import RoleState
from steward.core.stages.isolate import run_isolated
from steward.sdk import (
    BlobInfo,
    ChunkPart,
    FileChanged,
    FileInfo,
    FolderInfo,
    Message,
    StageModel,
    TextPart,
)

EMBED_BATCH_MAX = 256

OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


@dataclass(frozen=True)
class Context:
    stage: str
    blob: BlobInfo
    file: FileInfo
    folder: FolderInfo
    attempt: int
    root: str
    dbs: Databases = field(repr=False)
    renew: Callable[[], None] = field(repr=False)
    requires: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    optional: frozenset[str] = frozenset()
    """The stage's optional inputs that are available (their producer's result is usable)."""
    unavailable: frozenset[str] = frozenset()
    """Its other optional inputs: readable, but always empty."""
    roles: Mapping[str, RoleState] = field(default_factory=dict, repr=False)
    """The stage's model roles (all usable: the graph leaves out stages with others)."""
    gateway: Callable[[], Gateway] | None = field(default=None, repr=False)
    provenance: tuple[str, ...] = ()
    """Every present file with this content (sent with each model call)."""

    @property
    def path(self) -> Path:
        return Path(self.root) / self.file.relpath

    def open(self) -> BinaryIO:
        try:
            fd = os.open(self.path, OPEN_FLAGS)
        except FileNotFoundError as exc:
            raise FileChanged("file is gone") from exc
        except OSError as exc:
            if os.path.islink(self.path):
                raise FileChanged("file was replaced by a symlink") from exc
            raise
        try:
            self._check(os.fstat(fd))
        except BaseException:
            os.close(fd)
            raise
        return os.fdopen(fd, "rb")

    def unchanged(self) -> bool:
        """Whether the file still matches discovery's record (checked after `run`)."""
        try:
            self._check(os.stat(self.path, follow_symlinks=False))
        except (FileChanged, FileNotFoundError):
            return False
        return True

    def _check(self, st: os.stat_result) -> None:
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_size != self.file.size
            or st.st_mtime_ns != self.file.mtime_ns
        ):
            raise FileChanged("file changed since it was scanned")

    @contextmanager
    def index_read(self) -> Iterator[Connection]:
        with self.dbs.index.read() as conn:
            yield conn

    @contextmanager
    def index_write(self) -> Iterator[Connection]:
        with self.dbs.index.write() as conn:
            yield conn

    def renew_lease(self) -> None:
        self.renew()

    def run_isolated[T](
        self, fn: Callable[[BinaryIO], T], *, timeout_s: float, memory_mib: int
    ) -> T:
        return run_isolated(
            self.path,
            self.file.size,
            self.file.mtime_ns,
            fn,
            timeout_s=timeout_s,
            memory_mib=memory_mib,
            renew=self.renew,
        )

    # --- content index ----------------------------------------------------------------

    def _allowed(self, artifact: str, *, write: bool) -> bool:
        """Whether `artifact` can be read (False: an unavailable optional input)."""
        if write:
            allowed: tuple[str, ...] | frozenset[str] = self.produces
        else:
            allowed = (*self.requires, *self.optional, *self.unavailable, *self.produces)
        if artifact not in allowed:
            verb = "produce" if write else "read"
            raise ValueError(f"stage {self.stage} doesn't {verb} artifact {artifact!r}")
        return artifact not in self.unavailable

    def read_text(self, artifact: str = "text") -> list[TextPart]:
        if not self._allowed(artifact, write=False):
            return []
        with self.dbs.index.read() as conn:
            return content.read_text(conn, self.blob.content_hash, artifact)

    def write_text(self, parts: Sequence[TextPart], *, artifact: str = "text") -> None:
        self._allowed(artifact, write=True)
        with self.dbs.index.write() as conn:
            content.write_text(conn, self.blob.content_hash, artifact, parts)

    def read_chunks(self, artifact: str = "chunks") -> list[ChunkPart]:
        if not self._allowed(artifact, write=False):
            return []
        with self.dbs.index.read() as conn:
            return content.read_chunks(conn, self.blob.content_hash, artifact)

    def write_chunks(
        self, chunks: Sequence[ChunkPart], *, source: str, artifact: str = "chunks"
    ) -> None:
        self._allowed(artifact, write=True)
        with self.dbs.index.write() as conn:
            content.write_chunks(conn, self.blob.content_hash, artifact, source, chunks)

    # --- models ---------------------------------------------------------------------

    def model(self, role: str) -> StageModel:
        if role not in self.roles or self.gateway is None:
            raise ValueError(f"stage {self.stage} doesn't use role {role!r}")
        return RoleModel(self.roles[role], self.gateway(), self.provenance)

    def write_embeddings(
        self, vectors: Sequence[Sequence[float]], *, role: str, source: str = "chunks"
    ) -> None:
        self._allowed("embeddings", write=True)
        if role not in self.roles or self.gateway is None:
            raise ValueError(f"stage {self.stage} doesn't use role {role!r}")
        state = self.roles[role]
        if state.kind != "embed" or state.model_key is None:
            raise ValueError(f"role {role!r} isn't a usable embedding role")
        if source not in (*self.requires, *self.optional, *self.unavailable):
            raise ValueError(f"stage {self.stage} doesn't read artifact {source!r}")
        mapping = self.gateway().role_config(role)
        with self.dbs.index.write() as conn:
            if not vectors:  # no chunks: drop what an earlier run stored
                space = embeddings.find_space(conn, role, state.model_key)
                if space is not None:
                    embeddings.write(
                        conn, self.blob.content_hash, space=space, source=source, vectors=[]
                    )
                return
            dims = len(vectors[0])
            if mapping.dims is not None and mapping.dims != dims:
                raise embeddings.EmbeddingError(
                    f"role {role!r} is configured with dims {mapping.dims}, "
                    f"the model returned {dims}"
                )
            space = embeddings.ensure_space(
                conn, role=role, mapping=mapping, model_key=state.model_key, dims=dims
            )
            embeddings.write(
                conn, self.blob.content_hash, space=space, source=source, vectors=vectors
            )


@dataclass(frozen=True)
class RoleModel:
    """`StageModel` for one role: calls go through the gateway, for indexing, with the
    content's files as provenance."""

    state: RoleState
    gateway: Gateway
    provenance: tuple[str, ...]

    @property
    def role(self) -> str:
        return self.state.name

    @property
    def model(self) -> str:
        return self.state.model or ""

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self.state.kind != "embed":
            raise ValueError(f"role {self.role!r} isn't an embedding role")
        if isinstance(texts, str):
            raise TypeError("embed() takes a list of texts")
        prefix = self.gateway.role_config(self.role).document_prefix
        vectors: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH_MAX):
            batch = [prefix + t for t in texts[i : i + EMBED_BATCH_MAX]]
            result = self.gateway.call(
                self.role, EmbedRequest(batch), provenance=self.provenance, purpose="index"
            )
            got = result.response.vectors
            if len(got) != len(batch):
                raise embeddings.EmbeddingError(
                    f"the model returned {len(got)} vectors for {len(batch)} texts"
                )
            vectors.extend(got)
        return vectors

    def chat(self, messages: Sequence[Message], *, options: Mapping[str, Any] | None = None) -> str:
        if self.state.kind != "chat":
            raise ValueError(f"role {self.role!r} isn't a chat role")
        result = self.gateway.call(
            self.role,
            ChatRequest(list(messages), dict(options or {})),
            provenance=self.provenance,
            purpose="index",
        )
        return result.response.text

    def structured[M: BaseModel](
        self, schema: type[M], messages: Sequence[Message], *, retries: int = 2
    ) -> M:
        if self.state.kind != "chat":
            raise ValueError(f"role {self.role!r} isn't a chat role")

        def send(request: ChatRequest) -> str:
            result = self.gateway.call(
                self.role, request, provenance=self.provenance, purpose="index"
            )
            return result.response.text

        mapping = self.gateway.role_config(self.role)
        return structured_output.run(send, mapping, schema, messages, retries=retries)
