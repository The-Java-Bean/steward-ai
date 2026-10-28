# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Privacy: which files are sensitive or locked (plan §9, docs/adr/0004).

The model gateway (`steward.core.llm`), search, the stage planner and discovery all ask
this package, so the rules live in one place.
"""

from steward.core.privacy.classify import UNKNOWN, Classifier, FileClass, load_overrides, parent_of
from steward.core.privacy.overrides import Override, UnknownFileError, set_override
from steward.core.privacy.rules import PrivacyRules, matches

__all__ = [
    "UNKNOWN",
    "Classifier",
    "FileClass",
    "Override",
    "PrivacyRules",
    "UnknownFileError",
    "load_overrides",
    "matches",
    "parent_of",
    "set_override",
]
