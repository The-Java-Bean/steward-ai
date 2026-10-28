# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Pipeline stages: units of work that compute something from a file's content.

A stage declares the artifact kinds it reads (`requires`) and writes (`produces`). The
core builds a graph from every enabled stage, runs each stage once per unique content
(results are keyed by content hash, not path), in dependency order, and runs it again
when its `version` rises or something it depends on is recomputed
(docs/adr/0011-stage-framework.md).

Stages read files only through `StageContext.open()` (read-only) and store results in
their module's own `index.db` tables (`<module>_*`) through `StageContext.index_write()`.
A stage can run more than once for the same content, so writes must be idempotent
(upserts keyed by content hash).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import BinaryIO, Literal, Protocol

from sqlalchemy import Connection

from steward.sdk.jobs import Lane

IndexPolicy = Literal["full", "metadata", "none"]
"""How much of a folder steward-ai reads: contents, only names and metadata, or nothing."""
Sensitivity = Literal["normal", "local-only", "excluded"]
StageStatus = Literal["done", "skipped", "failed"]

ARTIFACT_PATTERN = r"^[a-z][a-z0-9_]*(:[a-z0-9_.-]+)?$"
"""Artifact kinds look like `text`, `text:ocr` or `embedding:<space>`."""


class FileChanged(Exception):
    """The file no longer matches what discovery recorded; the stage run is dropped and
    the file is rescanned. Raised by `StageContext.open()`; stages may raise it too."""


@dataclass(frozen=True, slots=True)
class BlobInfo:
    """One unique content, as discovery recorded it."""

    content_hash: str
    hash_kind: Literal["full", "sampled"]
    size: int
    mime: str
    kind: str
    """Coarse kind from the MIME type: `document`, `image`, `video`, `audio`, `text`,
    `archive` or `other`."""


@dataclass(frozen=True, slots=True)
class FileInfo:
    """A file that has this content (there may be several; the stage sees one)."""

    file_id: str
    library_id: str
    relpath: str
    size: int
    mtime_ns: int

    @property
    def name(self) -> str:
        return self.relpath.rsplit("/", 1)[-1]


@dataclass(frozen=True, slots=True)
class FolderInfo:
    """The effective settings of the folder that holds the file (from its folder type)."""

    type: str
    index: IndexPolicy
    sensitivity: Sensitivity
    locked: bool
    fields: tuple[str, ...]
    default_tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StageResult:
    """What `run` returns. Returning None means `StageResult()` (done)."""

    status: Literal["done", "skipped"] = "done"
    reason: str | None = None
    """Why it was skipped (recorded; shown in the UI). No file content."""
    model: str | None = None
    prompt_version: str | None = None


class StageContext(Protocol):
    """What a stage's `run` receives."""

    @property
    def stage(self) -> str: ...
    @property
    def blob(self) -> BlobInfo: ...
    @property
    def file(self) -> FileInfo: ...
    @property
    def folder(self) -> FolderInfo: ...
    @property
    def attempt(self) -> int: ...

    def open(self) -> BinaryIO:
        """Open the file read-only. Raises `FileChanged` if it changed since discovery."""
        ...

    def index_read(self) -> AbstractContextManager[Connection]:
        """A read transaction on `index.db` (to read artifacts from required stages)."""
        ...

    def index_write(self) -> AbstractContextManager[Connection]:
        """A write transaction on `index.db`, for this module's `<module>_*` tables."""
        ...

    def renew_lease(self) -> None:
        """Call during long work so the job isn't handed to another worker."""
        ...


StageRunner = Callable[[StageContext], StageResult | None]
"""Runs in a worker thread on the stage's lane. Raise to fail (retried with backoff;
`PermanentJobError` gives up at once)."""

AppliesTo = Callable[[BlobInfo, FileInfo, FolderInfo], bool]
"""Cheap, pure check whether a stage is meaningful for this content (MIME, size…).
No I/O: it runs for every file whenever the planner looks at it."""


def _always(blob: BlobInfo, file: FileInfo, folder: FolderInfo) -> bool:
    return True


@dataclass(frozen=True, slots=True)
class Stage:
    """A registered stage (built by `Registry.stage`)."""

    name: str
    version: int
    produces: tuple[str, ...]
    run: StageRunner = field(compare=False)
    requires: tuple[str, ...] = ()
    lane: Lane = "cpu"
    applies_to: AppliesTo = field(default=_always, compare=False)
    default: bool = True
    """Runs in every folder unless its type removes it; if False, only where a folder
    type adds it (`stages: {add: [...]}`)."""
    index: Literal["full", "metadata"] = "full"
    """The least a folder's `index` policy must allow: `full` stages read contents;
    `metadata` stages also run in metadata-only folders."""

    @property
    def short_name(self) -> str:
        """The name without the module prefix (`documents.ocr` -> `ocr`). Folder types
        may refer to a stage by either."""
        return self.name.split(".", 1)[-1]
