# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Folder types: named bundles of behavior, built-in and custom (docs/adr/0006).

A type's settings are all optional. Unset settings come from the type it `extends`,
and finally from `general`, which defines everything. Built-in types implicitly extend
`general`; the owner can override their settings but not delete them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from steward.sdk import IndexPolicy as IndexPolicy
from steward.sdk import Sensitivity as Sensitivity

HashPolicy = Literal["full", "sampled"]
SENSITIVITY_ORDER: tuple[Sensitivity, ...] = get_args(Sensitivity)

TYPE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def max_sensitivity(*levels: Sensitivity) -> Sensitivity:
    return max(levels, key=SENSITIVITY_ORDER.index)


class FolderTypeError(ValueError):
    """A folder type definition is invalid (unknown parent, cycle, bad name...)."""


class StageChanges(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()


class TypeSettings(BaseModel):
    """A folder type's own settings. `None` means "inherit"."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str | None = None
    """Shown in the UI and given to the AI for filing decisions."""
    index: IndexPolicy | None = None
    """Read contents (`full`), only names and metadata, or nothing."""
    hash: HashPolicy | None = None
    """`sampled` hashes size + mtime + three 1 MiB samples; for huge files."""
    stages: StageChanges | None = None
    """Pipeline stages to add or remove; accumulates along `extends`."""
    sensitivity: Sensitivity | None = None
    locked: bool | None = None
    """Contents only viewable in the Locked Folder view."""
    writable: bool | None = None
    shareable: bool | None = None
    triage_target: bool | None = None
    """Whether triage may propose moving files here."""
    visible: bool | None = None
    """Shown in the file manager (`ignored` folders aren't)."""
    sidecars: bool | None = None
    """Write XMP sidecars for photos here (the folder must be writable, and not locked)."""
    default_tags: tuple[str, ...] | None = None
    fields: tuple[str, ...] | None = None
    """Extra structured fields to extract, e.g. `vendor`, `amount`."""
    ignore: tuple[str, ...] | None = None
    """Glob patterns of file and folder names to skip entirely (e.g. `node_modules`)."""


class TypeDefinition(TypeSettings):
    """A custom type: settings plus the type it extends (default `general`)."""

    extends: str | None = Field(default=None)


@dataclass(frozen=True, slots=True)
class ResolvedType:
    """A type with every setting filled in."""

    name: str
    builtin: bool
    chain: tuple[str, ...]
    """This type, then each type it extends, ending with `general`."""
    description: str
    index: IndexPolicy
    hash: HashPolicy
    stages_add: frozenset[str]
    stages_remove: frozenset[str]
    sensitivity: Sensitivity
    locked: bool
    writable: bool
    shareable: bool
    triage_target: bool
    visible: bool
    sidecars: bool
    default_tags: tuple[str, ...]
    fields: tuple[str, ...]
    ignore: tuple[str, ...]


GENERAL = TypeSettings(
    description="Anything without a more specific type.",
    index="full",
    hash="full",
    stages=StageChanges(),
    sensitivity="normal",
    locked=False,
    writable=True,
    shareable=True,
    triage_target=True,
    visible=True,
    sidecars=False,
    default_tags=(),
    fields=(),
    ignore=(),
)

BUILTIN_TYPES: dict[str, TypeSettings] = {
    "general": GENERAL,
    "inbox": TypeSettings(
        description="New files waiting to be filed.",
        triage_target=False,
    ),
    "documents": TypeSettings(
        description="Documents: PDFs, office files, scans and text.",
    ),
    "media": TypeSettings(
        description="Photos and videos.",
        stages=StageChanges(add=("exif", "caption"), remove=("extract", "ocr")),
        sidecars=True,
    ),
    "notes": TypeSettings(
        description="Notes, mostly Markdown with frontmatter.",
    ),
    "projects": TypeSettings(
        description="Code and project files.",
        ignore=(
            ".git",
            "node_modules",
            "target",
            "build",
            "dist",
            ".venv",
            "venv",
            "__pycache__",
            ".tox",
            ".gradle",
            "vendor",
        ),
    ),
    "archive": TypeSettings(
        description="Old files kept for reference.",
        index="metadata",
        hash="sampled",
        triage_target=False,
    ),
    "software": TypeSettings(
        description="Installers, disk images and other software.",
        index="metadata",
        hash="sampled",
    ),
    "games": TypeSettings(
        description="Games and game files.",
        index="metadata",
        hash="sampled",
    ),
    "private": TypeSettings(
        description="Private files: local models only, viewable only in the Locked Folder.",
        sensitivity="local-only",
        locked=True,
        shareable=False,
        triage_target=False,
    ),
    "trash": TypeSettings(
        description="Deleted files, kept until you empty the trash.",
        index="metadata",
        sensitivity="local-only",
        shareable=False,
        triage_target=False,
    ),
    "app-managed": TypeSettings(
        description="Owned by another program; steward-ai only reads it.",
        writable=False,
        triage_target=False,
    ),
    "ignored": TypeSettings(
        description="Not indexed, not shown, never written.",
        index="none",
        writable=False,
        shareable=False,
        triage_target=False,
        visible=False,
    ),
}


def check_type_name(name: str) -> str:
    if not TYPE_NAME_PATTERN.match(name):
        raise FolderTypeError(
            f"folder type name {name!r} must be lowercase letters, digits, '-' or '_', "
            "starting with a letter"
        )
    return name


def resolve_types(
    overrides: Mapping[str, TypeSettings],
    custom: Mapping[str, TypeDefinition],
) -> dict[str, ResolvedType]:
    """Resolve every type.

    `overrides`: the owner's changes to built-in types (name -> settings).
    `custom`: custom types (name -> definition).
    """
    unknown_builtins = sorted(set(overrides) - set(BUILTIN_TYPES))
    if unknown_builtins:
        raise FolderTypeError(f"not built-in types: {', '.join(unknown_builtins)}")
    clashes = sorted(set(custom) & set(BUILTIN_TYPES))
    if clashes:
        raise FolderTypeError(f"custom types can't reuse built-in names: {', '.join(clashes)}")

    own: dict[str, TypeSettings] = {}
    parent: dict[str, str | None] = {}
    for name, base in BUILTIN_TYPES.items():
        own[name] = _merge(base, overrides.get(name))
        parent[name] = None if name == "general" else "general"
    for name, definition in custom.items():
        check_type_name(name)
        extends = definition.extends or "general"
        own[name] = TypeSettings(**definition.model_dump(exclude={"extends"}))
        parent[name] = extends

    resolved: dict[str, ResolvedType] = {}
    for name in own:
        chain = _chain(name, parent)
        resolved[name] = _resolve(name, chain, own)
    return resolved


def _merge(base: TypeSettings, override: TypeSettings | None) -> TypeSettings:
    if override is None:
        return base
    changes = override.model_dump(exclude_none=True)
    if "stages" in changes:
        changes["stages"] = override.stages  # keep the model, not a dict
    return base.model_copy(update=changes)


def _chain(name: str, parent: Mapping[str, str | None]) -> tuple[str, ...]:
    chain = [name]
    while (nxt := parent[chain[-1]]) is not None:
        if nxt not in parent:
            raise FolderTypeError(f"folder type {chain[-1]!r} extends unknown type {nxt!r}")
        if nxt in chain:
            raise FolderTypeError(
                f"folder types extend each other in a cycle: {' -> '.join([*chain, nxt])}"
            )
        chain.append(nxt)
    return tuple(chain)


_SCALARS = (
    "description",
    "index",
    "hash",
    "sensitivity",
    "locked",
    "writable",
    "shareable",
    "triage_target",
    "visible",
    "sidecars",
    "default_tags",
    "fields",
    "ignore",
)


def _resolve(name: str, chain: tuple[str, ...], own: Mapping[str, TypeSettings]) -> ResolvedType:
    values: dict[str, object] = {}
    for field in _SCALARS:
        for link in chain:
            value = getattr(own[link], field)
            if value is not None:
                values[field] = value
                break
    add: set[str] = set()
    remove: set[str] = set()
    for link in reversed(chain):  # general first, this type last
        stages = own[link].stages
        if stages is None:
            continue
        add = (add - set(stages.remove)) | set(stages.add)
        remove = (remove - set(stages.add)) | set(stages.remove)
    return ResolvedType(
        name=name,
        builtin=name in BUILTIN_TYPES,
        chain=chain,
        stages_add=frozenset(add),
        stages_remove=frozenset(remove),
        **values,  # type: ignore[arg-type]
    )
