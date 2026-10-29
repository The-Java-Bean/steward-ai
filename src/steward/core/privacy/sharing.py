# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Whether a path may be shared (plan §14.4): the one rule shares and share pages use.

A path may be shared when it is visible in normal views (not locked, not in a hidden,
trash or copy-holding folder), its sensitivity is `normal` (folder types, `privacy.yaml`
rules and per-file overrides all count) and its folder's type is shareable. The path is
judged both as a file (by its parent folder) and as a folder (by its own settings), so
the answer doesn't depend on what is on disk right now.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import Connection, select

from steward.core.db.schema import file_sensitivity, files
from steward.core.folders.policy import Policy
from steward.core.folders.types import Sensitivity, max_sensitivity
from steward.core.privacy.classify import Classifier, parent_of
from steward.core.privacy.rules import PrivacyRules
from steward.core.search import Visibility
from steward.sdk.actions import PathPrivacy

HIDDEN = PathPrivacy("local-only", locked=True, hidden=True, shareable=False, reason="Not found.")
"""For paths that don't parse or name an unknown library."""


@dataclass
class SharingRules:
    """Built from one policy snapshot; caches folder settings and answers."""

    policy: Policy
    rules: PrivacyRules
    overrides: Mapping[tuple[str, str], Sensitivity] = field(default_factory=dict)
    """(library_id, relpath) -> per-file override."""
    _classifier: Classifier = field(init=False)
    _visibility: Visibility = field(init=False)
    _cache: dict[tuple[str, str], PathPrivacy] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._classifier = Classifier(self.policy, self.rules)
        self._visibility = Visibility(self.policy)

    @classmethod
    def load(cls, conn: Connection, policy: Policy, rules: PrivacyRules) -> SharingRules:
        rows = conn.execute(
            select(files.c.library_id, files.c.relpath, file_sensitivity.c.sensitivity).join(
                file_sensitivity, file_sensitivity.c.file_id == files.c.file_id
            )
        )
        return cls(policy, rules, {(r.library_id, r.relpath): r.sensitivity for r in rows})

    def check(self, library_id: str, relpath: str) -> PathPrivacy:
        key = (library_id, relpath)
        found = self._cache.get(key)
        if found is None:
            found = self._cache[key] = self._check(library_id, relpath)
        return found

    def _check(self, library_id: str, relpath: str) -> PathPrivacy:
        if library_id not in self.policy.libraries:
            return HIDDEN
        own = self._classifier.folder_settings(library_id, relpath)
        parent = self._classifier.folder_settings(library_id, parent_of(relpath))
        if own is None or parent is None:
            return HIDDEN
        level = max_sensitivity(
            own.sensitivity,
            parent.sensitivity,
            self.rules.level_for(relpath),
            self.overrides.get((library_id, relpath), "normal"),
        )
        locked = own.locked or parent.locked
        hidden = self._visibility.is_hidden(library_id, relpath)
        reason: str | None = None
        if locked:
            reason = "It is in a locked folder."
        elif hidden:
            reason = "It is in a hidden folder."
        elif level == "excluded":
            reason = "It is excluded from steward-ai."
        elif level == "local-only":
            reason = "It is marked local-only."
        elif not (own.shareable and parent.shareable):
            kind = own.type.name if not own.shareable else parent.type.name
            reason = f"Its folder type ({kind}) doesn't allow sharing."
        return PathPrivacy(level, locked, hidden, reason is None, reason)
