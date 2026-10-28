# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Libraries, folder types, folder identities, mappings and roles (docs/adr/0006)."""

from steward.core.folders.paths import FolderRef, PathError, ancestors, is_within, normalize_relpath
from steward.core.folders.policy import (
    ROLES,
    EffectiveSettings,
    FolderMapping,
    Library,
    Policy,
    Role,
)
from steward.core.folders.store import FolderError, PermanentLockError, load_policy
from steward.core.folders.types import (
    BUILTIN_TYPES,
    FolderTypeError,
    ResolvedType,
    Sensitivity,
    StageChanges,
    TypeDefinition,
    TypeSettings,
    resolve_types,
)

__all__ = [
    "BUILTIN_TYPES",
    "ROLES",
    "EffectiveSettings",
    "FolderError",
    "FolderMapping",
    "FolderRef",
    "FolderTypeError",
    "Library",
    "PathError",
    "PermanentLockError",
    "Policy",
    "ResolvedType",
    "Role",
    "Sensitivity",
    "StageChanges",
    "TypeDefinition",
    "TypeSettings",
    "ancestors",
    "is_within",
    "load_policy",
    "normalize_relpath",
    "resolve_types",
]
