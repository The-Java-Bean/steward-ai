# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""What a file's privacy class is: its sensitivity and whether it's locked.

A file's sensitivity is the highest of its folder's effective sensitivity (folder types,
carried down, role folders), the `privacy.yaml` rules for its path, and its per-file
override. Rules only ever raise it. Anything that can't be resolved (unknown file,
removed library) is treated as `local-only` **and locked**, so it can't reach a chat
surface, a share or a remote model (fail closed, plan §3.13).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from sqlalchemy import Connection, select

from steward.core.db.schema import file_sensitivity, files
from steward.core.folders.paths import FolderRef, PathError
from steward.core.folders.policy import EffectiveSettings, Policy
from steward.core.folders.store import load_policy
from steward.core.folders.types import Sensitivity, max_sensitivity
from steward.core.privacy.rules import PrivacyRules


@dataclass(frozen=True, slots=True)
class FileClass:
    sensitivity: Sensitivity
    locked: bool
    visible: bool
    shareable: bool
    known: bool = True


UNKNOWN = FileClass("local-only", locked=True, visible=False, shareable=False, known=False)


def parent_of(relpath: str) -> str:
    return relpath.rsplit("/", 1)[0] if "/" in relpath else ""


@dataclass
class Classifier:
    """Built from one policy snapshot; cheap to make, caches folder settings."""

    policy: Policy
    rules: PrivacyRules
    overrides: Mapping[str, Sensitivity] = field(default_factory=dict)
    _folders: dict[tuple[str, str], EffectiveSettings | None] = field(default_factory=dict)

    @classmethod
    def load(cls, conn: Connection, rules: PrivacyRules) -> Classifier:
        return cls(load_policy(conn), rules, load_overrides(conn))

    def folder_settings(self, library_id: str, relpath: str) -> EffectiveSettings | None:
        key = (library_id, relpath)
        if key not in self._folders:
            try:
                self._folders[key] = self.policy.settings_for(FolderRef(library_id, relpath))
            except (PathError, KeyError):
                self._folders[key] = None
        return self._folders[key]

    def for_folder(self, library_id: str, relpath: str) -> FileClass:
        settings = self.folder_settings(library_id, relpath)
        if settings is None:
            return UNKNOWN
        level = max_sensitivity(settings.sensitivity, self.rules.level_for(relpath))
        return FileClass(
            level,
            settings.locked,
            settings.visible,
            settings.shareable and level == "normal",
        )

    def for_path(self, library_id: str, relpath: str, *, file_id: str | None = None) -> FileClass:
        settings = self.folder_settings(library_id, parent_of(relpath))
        if settings is None:
            return UNKNOWN
        level = max_sensitivity(
            settings.sensitivity,
            self.rules.level_for(relpath),
            self.overrides.get(file_id, "normal") if file_id else "normal",
        )
        return FileClass(
            level,
            settings.locked,
            settings.visible,
            settings.shareable and level == "normal",
        )

    def for_files(self, conn: Connection, file_ids: Iterable[str]) -> dict[str, FileClass]:
        """Every requested id gets a class; ids that aren't known files get `UNKNOWN`."""
        wanted = list(dict.fromkeys(file_ids))
        found: dict[str, FileClass] = dict.fromkeys(wanted, UNKNOWN)
        for start in range(0, len(wanted), 500):
            batch = wanted[start : start + 500]
            rows = conn.execute(
                select(files.c.file_id, files.c.library_id, files.c.relpath).where(
                    files.c.file_id.in_(batch)
                )
            )
            for row in rows:
                found[row.file_id] = self.for_path(row.library_id, row.relpath, file_id=row.file_id)
        return found


def load_overrides(conn: Connection) -> dict[str, Sensitivity]:
    return {
        row.file_id: row.sensitivity
        for row in conn.execute(select(file_sensitivity.c.file_id, file_sensitivity.c.sensitivity))
    }
