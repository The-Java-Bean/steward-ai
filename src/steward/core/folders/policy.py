# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Effective folder settings: which type applies to a folder, and what it allows.

A folder's type is the type of its nearest mapped ancestor-or-self; a library root
with no mapping is `general`. Settings come from that type, with one rule on top:
**restrictions can't be loosened by mapping a subfolder.** Sensitivity is the highest
along the path, and `locked`, not `writable`, not `shareable` and not `visible` all
carry down to everything inside. Folders holding the `trash` or `edit_backups` role
keep copies of anything (including private files), so they and their contents are at
least `local-only`, never shared and never triage targets.

`Policy` is an in-memory snapshot loaded from state.db (see `store.load_policy`);
resolving settings does no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, get_args

from steward.core.folders.paths import FolderRef, PathError, ancestors, is_within
from steward.core.folders.types import (
    HashPolicy,
    IndexPolicy,
    ResolvedType,
    Sensitivity,
    max_sensitivity,
)

Role = Literal["trash", "edit_backups", "inbox", "digest_output"]
ROLES: tuple[Role, ...] = get_args(Role)
COPY_HOLDING_ROLES: tuple[Role, ...] = ("trash", "edit_backups")
"""Roles whose folders receive copies of files from anywhere."""
WRITABLE_ROLES: tuple[Role, ...] = ROLES
"""Every role's folder must be writable (steward-ai writes into all of them)."""


@dataclass(frozen=True, slots=True)
class Library:
    library_id: str
    name: str
    root: str
    """Absolute path of the library root as seen by steward-ai (inside the container)."""
    position: int


@dataclass(frozen=True, slots=True)
class FolderMapping:
    folder_id: int
    ref: FolderRef
    type_name: str | None
    """None: no type of its own (only a lock override)."""
    locked: bool | None
    """None: follow the type; True/False: the owner's override for this folder."""
    permanent: bool


@dataclass(frozen=True, slots=True)
class EffectiveSettings:
    ref: FolderRef
    type: ResolvedType
    type_from: str | None
    """Relative path of the mapping that set the type; None if it's the library default."""
    index: IndexPolicy
    hash: HashPolicy
    stages_add: frozenset[str]
    stages_remove: frozenset[str]
    sensitivity: Sensitivity
    locked: bool
    locked_by: str | None
    """Relative path of the folder whose lock applies here (this folder or an ancestor)."""
    permanent: bool
    writable: bool
    shareable: bool
    triage_target: bool
    visible: bool
    sidecars: bool
    default_tags: tuple[str, ...]
    fields: tuple[str, ...]
    ignore: tuple[str, ...]
    notes: tuple[str, ...]
    """Human-readable reasons where a setting differs from the type's own value."""


@dataclass(frozen=True)
class Policy:
    libraries: Mapping[str, Library]
    types: Mapping[str, ResolvedType]
    mappings: Mapping[FolderRef, FolderMapping]
    roles: Mapping[Role, FolderRef]

    def library(self, library_id: str) -> Library:
        try:
            return self.libraries[library_id]
        except KeyError:
            raise PathError(f"unknown library {library_id!r}") from None

    def role_at(self, ref: FolderRef) -> list[Role]:
        """Roles whose folder is `ref` or contains it."""
        return [
            role
            for role, folder in self.roles.items()
            if folder.library_id == ref.library_id and is_within(ref.relpath, folder.relpath)
        ]

    def settings_for(self, ref: FolderRef) -> EffectiveSettings:
        self.library(ref.library_id)
        general = self.types["general"]
        current = general
        type_from: str | None = None
        sensitivity: Sensitivity = general.sensitivity
        locked, locked_by, permanent = False, None, False
        writable = shareable = visible = True
        notes: list[str] = []

        for path in ancestors(ref.relpath):
            mapping = self.mappings.get(FolderRef(ref.library_id, path))
            if mapping is not None and mapping.type_name is not None:
                current = self.types[mapping.type_name]
                type_from = path
            # A lock decision is made only where a folder is mapped: its own override,
            # else its own type. Inherited types don't re-decide, so unlocking a
            # locked-type folder unlocks its contents too. Once locked, always locked.
            decides: bool | None = None
            if mapping is not None and mapping.locked is not None:
                decides = mapping.locked
            elif (mapping is not None and mapping.type_name is not None) or path == "":
                decides = current.locked
            if decides and not locked:
                locked, locked_by = True, path
            if mapping is not None and mapping.permanent:
                permanent = True
            sensitivity = max_sensitivity(sensitivity, current.sensitivity)
            writable = writable and current.writable
            shareable = shareable and current.shareable
            visible = visible and current.visible

        if sensitivity != current.sensitivity:
            notes.append(f"sensitivity {sensitivity}: inherited from a parent folder")
        if locked and not current.locked:
            notes.append(f"locked by {FolderRef(ref.library_id, locked_by or '')}")
        if not writable and current.writable:
            notes.append("read-only: a parent folder isn't writable")
        if not visible and current.visible:
            notes.append("hidden: a parent folder is hidden")

        triage_target = current.triage_target
        for role in self.role_at(ref):
            if role in COPY_HOLDING_ROLES:
                if sensitivity == "normal":
                    sensitivity = "local-only"
                shareable = triage_target = False
                notes.append(f"holds the {role} role: at least local-only, never shared")
        if locked:
            if shareable:
                notes.append("locked: can't be shared")
            shareable = triage_target = False
        if not writable:
            triage_target = False

        return EffectiveSettings(
            ref=ref,
            type=current,
            type_from=type_from,
            index=current.index,
            hash=current.hash,
            stages_add=current.stages_add,
            stages_remove=current.stages_remove,
            sensitivity=sensitivity,
            locked=locked,
            locked_by=locked_by,
            permanent=permanent,
            writable=writable,
            shareable=shareable,
            triage_target=triage_target,
            visible=visible,
            sidecars=current.sidecars,
            default_tags=current.default_tags,
            fields=current.fields,
            ignore=current.ignore,
            notes=tuple(notes),
        )
