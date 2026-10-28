# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Suggested folder types for the setup wizard.

Only suggestions: the owner accepts, changes or skips each one, and nothing here
decides behavior. A suggestion comes from the folder's name (with number prefixes such
as `10-` removed), else from marker files (`.git`, `.obsidian`), else from the kinds of
the first few hundred files inside. Walking is bounded and never follows symlinks.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from steward.core.discovery.hashing import detect_mime, kind_of

MAX_ENTRIES = 2000
"""Directory entries looked at per folder, at most."""
MAX_DEPTH = 3

_NAME_HINTS: dict[str, tuple[str, ...]] = {
    "documents": ("documents", "docs", "paperwork", "papers", "records", "scans", "pdfs"),
    "media": (
        "media",
        "photos",
        "pictures",
        "images",
        "camera",
        "videos",
        "movies",
        "music",
        "audio",
    ),
    "notes": ("notes", "vault", "journal", "obsidian", "wiki"),
    "projects": ("projects", "code", "src", "dev", "repos", "git", "workspace"),
    "archive": ("archive", "archives", "old", "backup", "backups"),
    "software": ("software", "apps", "installers", "programs", "iso", "isos"),
    "games": ("games", "roms", "steam"),
    "private": ("private", "personal", "secret", "secrets"),
    "inbox": ("inbox", "incoming", "downloads", "unsorted", "to-sort", "tosort"),
}
_BY_NAME = {alias: type_name for type_name, names in _NAME_HINTS.items() for alias in names}
_PREFIX = re.compile(r"^[\d\s._-]+")
_MARKERS: dict[str, str] = {
    ".git": "projects",
    "pyproject.toml": "projects",
    "package.json": "projects",
    "Cargo.toml": "projects",
    "go.mod": "projects",
    ".obsidian": "notes",
}
_SOFTWARE_SUFFIXES = (".iso", ".img", ".dmg", ".exe", ".msi", ".deb", ".rpm", ".appimage")


@dataclass(frozen=True, slots=True)
class Suggestion:
    type_name: str | None
    """None: no suggestion (the wizard offers "skip" first)."""
    reason: str


def _name_key(name: str) -> str:
    return _PREFIX.sub("", name).strip().casefold().replace(" ", "-").replace("_", "-")


def suggest_by_name(name: str) -> str | None:
    key = _name_key(name)
    for candidate in (key, key + "s", key.removesuffix("s")):
        if candidate in _BY_NAME:
            return _BY_NAME[candidate]
    return None


def suggest(path: Path) -> Suggestion:
    """A suggested type for the folder at `path`."""
    by_name = suggest_by_name(path.name)
    if by_name is not None:
        return Suggestion(by_name, f"the name {path.name!r}")

    kinds: Counter[str] = Counter()
    software = 0
    seen = 0
    stack = [(path, 0)]
    while stack and seen < MAX_ENTRIES:
        folder, depth = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if seen > MAX_ENTRIES:
                break
            if entry.name in _MARKERS:
                return Suggestion(_MARKERS[entry.name], f"it contains {entry.name}")
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth + 1 < MAX_DEPTH and not entry.name.startswith("."):
                        stack.append((Path(entry.path), depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    if entry.name.lower().endswith(_SOFTWARE_SUFFIXES):
                        software += 1
                    kinds[kind_of(detect_mime(entry.name, b""))] += 1
            except OSError:
                continue

    total = sum(kinds.values())
    if total == 0:
        return Suggestion(None, "it's empty")
    if software / total >= 0.5:
        return Suggestion("software", "mostly installers and disk images")
    media = kinds["image"] + kinds["video"] + kinds["audio"]
    if media / total >= 0.6:
        return Suggestion("media", "mostly photos, videos or audio")
    documents = kinds["document"] + kinds["text"]
    if documents / total >= 0.6:
        return Suggestion("documents", "mostly documents and text")
    return Suggestion(None, "a mix of file kinds")
