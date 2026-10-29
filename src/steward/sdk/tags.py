# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Tags (plan §13.8, docs/adr/0025).

The core keeps the vocabulary and your decisions; a file's tags are computed from three
sources: the tags you added (`user`), its folder type's `default_tags` (`rule`) and the
tags the AI chose for its content (`ai`, written by a stage with
`StageContext.write_tags`), minus the tags you removed from it. Names are paths
(`finance/receipt`): a tag also stands for the tags under it in searches.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

TagStatus = Literal["active", "ai-created", "deprecated"]
"""`active`: in use. `ai-created`: made by an AI, waiting for your review (usable
meanwhile). `deprecated`: kept on files that have it, never chosen again."""
TagSource = Literal["user", "rule", "ai"]

TAGS_ARTIFACT = "tags"
"""The artifact kind of the stage that chooses tags (one stage may produce it)."""


@dataclass(frozen=True, slots=True)
class TagInfo:
    """A tag of the vocabulary."""

    name: str
    status: TagStatus
    description: str = ""
    aliases: tuple[str, ...] = ()
    """Old names (renames) and merged tags that now mean this one."""
    files: int | None = None
    """How many files have it, in the normal scope (when counted)."""

    @property
    def depth(self) -> int:
        return self.name.count("/")

    @property
    def leaf(self) -> str:
        return self.name.rsplit("/", 1)[-1]


@dataclass(frozen=True, slots=True)
class FileTag:
    """One of a file's tags and why it has it."""

    name: str
    source: TagSource
    status: TagStatus = "active"


class Tags(Protocol):
    """The vocabulary, for module pages (changes go through the `tag.*` action types)."""

    def vocabulary(self, *, counts: bool = True) -> list[TagInfo]:
        """Every tag but aliases, by name; with `counts`, the files having each one."""
        ...

    def retag(self, *, everything: bool = False) -> int:
        """Run the tag-choosing stage again where it was skipped (or, with `everything`,
        for every file: one model call each). Returns how many results were dropped."""
        ...
