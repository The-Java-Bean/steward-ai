# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Tags (plan §13.8, docs/adr/0025): the vocabulary and your decisions in `state.db`,
the AI's choices per content in `index.db`, effective tags computed from both and the
folder types' `default_tags`."""

from steward.core.tags.names import TagError, normalize
from steward.core.tags.store import USABLE, VOCABULARY_CHANGED, Tag

__all__ = ["USABLE", "VOCABULARY_CHANGED", "Tag", "TagError", "normalize"]
