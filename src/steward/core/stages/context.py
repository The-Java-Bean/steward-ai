# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The `StageContext` a stage's `run` receives: read-only file access and `index.db`."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import Connection

from steward.core.db import Databases
from steward.sdk import BlobInfo, FileChanged, FileInfo, FolderInfo

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
