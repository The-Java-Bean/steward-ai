# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Pipeline stages: units of work that compute something from a file's content.

A stage declares the artifact kinds it reads (`requires`) and writes (`produces`). The
core builds a graph from every enabled stage, runs each stage once per unique content
(results are keyed by content hash, not path), in dependency order, and runs it again
when its `version` rises or something it depends on is recomputed
(docs/adr/0011-stage-framework.md).

Stages read files only through `StageContext.open()` or `StageContext.run_isolated()`
(read-only) and store results either in the core's content index (`write_text`,
`write_chunks`: searchable, privacy-filtered by the core) or in their module's own
`index.db` tables (`<module>_*`) through `StageContext.index_write()`. A stage can run
more than once for the same content, so writes must be idempotent (upserts keyed by
content hash; the content-index writes replace what the stage wrote before).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
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


MATCH_MARKERS = ("\ue000", "\ue001")
"""Private-use characters the core uses to mark matches in search snippets. They're
removed from every text written to the content index."""


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
class TextPart:
    """One page or section of extracted text (artifact kinds `text`, `text:ocr`…)."""

    body: str
    page: int | None = None
    """1-based page number, for paged documents."""
    heading: str | None = None
    """The heading the section starts with (or sits under), if the format has headings."""


@dataclass(frozen=True, slots=True)
class ChunkPart:
    """One retrieval unit: what full-text search (and later embeddings) index."""

    body: str
    page: int | None = None
    heading: str | None = None


IsolationReason = Literal["timed_out", "too_big", "crashed", "error"]


class IsolationError(Exception):
    """`run_isolated` didn't return a result. `reason` is `timed_out`, `too_big` (memory
    limit), `crashed` (the child died) or `error` (the function raised; `error_type` is
    the exception's class name). Messages never carry file content."""

    def __init__(self, reason: IsolationReason, error_type: str | None = None) -> None:
        self.reason = reason
        self.error_type = error_type
        super().__init__(f"{reason}: {error_type}" if error_type else reason)


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

    def run_isolated[T](
        self,
        fn: Callable[[BinaryIO], T],
        *,
        timeout_s: float,
        memory_mib: int,
    ) -> T:
        """Run `fn(file)` in a separate process with the file opened read-only there, so a
        hostile file can't hang, crash or bloat the worker. `fn` must be a module-level
        function (it's pickled by name) and its result picklable. Raises `FileChanged`,
        or `IsolationError` when there's no result."""
        ...

    def read_text(self, artifact: str = "text") -> list[TextPart]:
        """The text a required stage stored for this content (`artifact` must be in this
        stage's `requires`). Empty if it found none."""
        ...

    def write_text(self, parts: Sequence[TextPart], *, artifact: str = "text") -> None:
        """Store this content's text as `artifact` (one of this stage's `produces`),
        replacing what was stored before."""
        ...

    def read_chunks(self, artifact: str = "chunks") -> list[ChunkPart]:
        """The chunks stored for this content as `artifact` (in `requires` or `produces`)."""
        ...

    def write_chunks(
        self, chunks: Sequence[ChunkPart], *, source: str, artifact: str = "chunks"
    ) -> None:
        """Store this content's chunks as `artifact` (one of this stage's `produces`),
        replacing what was stored before; `source` names the text artifact they came
        from. Chunks are full-text searchable at once."""
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
