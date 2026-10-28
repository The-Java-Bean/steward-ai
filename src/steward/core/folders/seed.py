# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Import `libraries.yaml` and `folders.yaml` into state.db.

The database is the source of truth (the UI edits it); these files seed it and can be
re-imported for reproducible setups. Importing only adds and updates: it never removes
a library, type, mapping or role, and it never removes a permanent lock.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Connection

from steward.core.config import FoldersConfig, LibrariesConfig, MappingEntry
from steward.core.folders import store
from steward.core.folders.paths import FolderRef
from steward.core.folders.store import FolderError, PermanentLockError
from steward.core.folders.types import BUILTIN_TYPES, FolderTypeError, TypeDefinition


@dataclass
class ImportReport:
    libraries_added: list[str] = field(default_factory=list)
    types_defined: list[str] = field(default_factory=list)
    mappings: int = 0
    roles: list[str] = field(default_factory=list)
    default_roles: list[str] = field(default_factory=list)


def import_seed(
    conn: Connection,
    *,
    libraries: LibrariesConfig,
    folders: FoldersConfig,
    actor: str = "system",
) -> ImportReport:
    """Apply both files in one transaction (the caller's)."""
    report = ImportReport()
    for library_id, entry in libraries.libraries.items():
        if store.upsert_library(conn, library_id, root=entry.path, name=entry.name, actor=actor):
            report.libraries_added.append(library_id)

    for name, definition in _parents_first(folders.types):
        if store.define_type(conn, name, definition, actor=actor) != "unchanged":
            report.types_defined.append(name)

    for library_id, entries in folders.mappings.items():
        for relpath, raw in entries.items():
            mapping = MappingEntry(type=raw) if isinstance(raw, str) else raw
            _apply_mapping(conn, FolderRef.of(library_id, relpath), mapping, actor)
            report.mappings += 1

    for role, target in folders.roles.items():
        store.set_role(conn, role, FolderRef.parse(target), actor=actor)
        report.roles.append(role)

    report.default_roles = list(store.ensure_default_roles(conn, actor=actor))
    return report


def _apply_mapping(conn: Connection, ref: FolderRef, entry: MappingEntry, actor: str) -> None:
    if entry.permanent and entry.locked is False:
        raise FolderError(f"{ref}: a permanent lock needs `locked: true` (or no `locked`)")
    if entry.type is not None:
        store.set_type(conn, ref, entry.type, actor=actor)
    if entry.locked or entry.permanent:
        store.lock(conn, ref, permanent=entry.permanent, actor=actor)
    elif entry.locked is False:
        try:
            store.unlock(conn, ref, actor=actor)
        except PermanentLockError as exc:
            raise FolderError(f"{ref} is permanently locked; the import can't unlock it") from exc


def _parents_first(types: dict[str, TypeDefinition]) -> list[tuple[str, TypeDefinition]]:
    ordered: list[tuple[str, TypeDefinition]] = []
    done: set[str] = set()
    pending = dict(types)
    while pending:
        ready = [
            name
            for name, d in pending.items()
            if name in BUILTIN_TYPES
            or d.extends is None
            or d.extends not in pending
            or d.extends in done
        ]
        if not ready:
            raise FolderTypeError(
                f"folder types extend each other in a cycle: {', '.join(sorted(pending))}"
            )
        for name in sorted(ready):
            ordered.append((name, pending.pop(name)))
            done.add(name)
    return ordered
