# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The `StageContext` a stage's `run` receives: read-only file access (directly or in an
isolated child process), the content index and `index.db`."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import Connection

from steward.core import content
from steward.core.db import Databases
from steward.core.stages.isolate import run_isolated
from steward.sdk import BlobInfo, ChunkPart, FileChanged, FileInfo, FolderInfo, TextPart

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
