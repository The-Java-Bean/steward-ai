# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Tag names: `/`-separated paths (`finance/receipt`), compared casefolded."""

from __future__ import annotations

import unicodedata

SEPARATOR = "/"
MAX_SEGMENT = 64
MAX_DEPTH = 6
MAX_NAME = 200
FORBIDDEN = frozenset(':"\\,')


class TagError(ValueError):
    """A bad tag name, or a vocabulary change that isn't possible."""


def normalize(name: str) -> str:
    """The name as stored: NFC, spaces collapsed, no blank segments. Raises `TagError`."""
    name = unicodedata.normalize("NFC", name)
    segments = [" ".join(seg.split()) for seg in name.strip().strip(SEPARATOR).split(SEPARATOR)]
    if not segments or any(not seg for seg in segments):
        raise TagError("a tag name can't be empty or have an empty part between '/'")
    if len(segments) > MAX_DEPTH:
        raise TagError(f"a tag can be at most {MAX_DEPTH} levels deep")
    for seg in segments:
        if len(seg) > MAX_SEGMENT:
            raise TagError(f"each part of a tag name can be at most {MAX_SEGMENT} characters")
        bad = {ch for ch in seg if ch in FORBIDDEN or unicodedata.category(ch).startswith("C")}
        if bad:
            raise TagError("a tag name can't contain " + ", ".join(sorted(map(repr, bad))))
    result = SEPARATOR.join(segments)
    if len(result) > MAX_NAME:
        raise TagError(f"a tag name can be at most {MAX_NAME} characters")
    return result


def key(name: str) -> str:
    """The comparison key of a normalized name."""
    return name.casefold()


def is_within(child_key: str, parent_key: str) -> bool:
    """`finance/receipt` is within `finance` (and within itself)."""
    return child_key == parent_key or child_key.startswith(parent_key + SEPARATOR)


def parent(name: str) -> str | None:
    return name.rsplit(SEPARATOR, 1)[0] if SEPARATOR in name else None
