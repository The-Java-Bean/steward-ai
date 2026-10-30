# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""What a job may see of the libraries, and how a job on the `executor` lane changes them.

`JobContext.library` pages through files with their folder settings, effective tags and
texts. It never returns anything from locked, hidden or excluded places, so a module
can't leak them by forgetting to check. `JobContext.actions` runs action types as
`module:<name>`, journaled like everything else; it can't confirm a password, so
sensitive actions and anything touching a locked folder are refused.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from steward.sdk.stages import BlobInfo, FileInfo, FolderInfo
from steward.sdk.tags import FileTag


@dataclass(frozen=True, slots=True)
class LibraryFile:
    file: FileInfo
    blob: BlobInfo
    folder: FolderInfo
    tags: tuple[FileTag, ...] = ()
    """Its effective tags: what the file page shows."""
    texts: Mapping[str, str] = field(default_factory=dict)
    """The text of each artifact asked for that this content has."""

    @property
    def path(self) -> str:
        return f"{self.file.library_id}:{self.file.relpath}"


@dataclass(frozen=True, slots=True)
class LibraryPage:
    files: Sequence[LibraryFile]
    next_after: str | None
    """Pass as `after` for the next page; None at the end. A page can be short, or even
    empty, without being the last."""


class LibraryAccess(Protocol):
    def revision(self) -> str:
        """Changes whenever files, folders, tags or stage results may have changed (not
        only then). A job that compares everything can skip a run if this is what it was
        the last time; read it *before* looking at anything."""
        ...

    def rebuilding(self) -> bool:
        """The index is being rebuilt (`steward reindex`), so stage results may be missing
        that will be back later. A job that would act on something being absent waits."""
        ...

    def files(
        self,
        *,
        kind: str,
        after: str | None = None,
        limit: int = 500,
        texts: tuple[str, ...] = (),
    ) -> LibraryPage:
        """Files of a `kind` (`image`...) in file-id order, with `texts` (artifact kinds)."""
        ...

    def read(self, path: str, *, max_bytes: int) -> tuple[bytes, str] | None:
        """The content and content hash of the file at `path`; None if nothing is there.
        Raises `ActionRefused` for a symlink, a folder or a file over `max_bytes`."""
        ...


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    entry_id: int
    """The journal entry."""
    result: Mapping[str, Any]


class ActionRunner(Protocol):
    def run(self, action: str, params: Mapping[str, Any]) -> ActionOutcome:
        """Run an action type. Raises `ActionRefused` if it isn't allowed, or `ActionFailed`
        if applying it failed."""
        ...
