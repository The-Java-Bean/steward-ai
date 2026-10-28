# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Which names discovery skips: temporary files (everywhere) and a folder type's
`ignore` list (in folders of that type)."""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase


def is_ignored(relpath: str, patterns: Iterable[str]) -> bool:
    """True if `relpath`'s name matches a pattern. A pattern with a `/` (e.g.
    `.obsidian/workspace*.json`) matches the end of the path instead."""
    name = relpath.rsplit("/", 1)[-1]
    for pattern in patterns:
        if "/" in pattern:
            depth = pattern.count("/") + 1
            tail = "/".join(relpath.split("/")[-depth:])
            if fnmatchcase(tail, pattern):
                return True
        elif fnmatchcase(name, pattern):
            return True
    return False
