# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Library-relative paths and folder references.

Paths are stored as `(library_id, relpath)`: POSIX separators, NFC-normalized, no
leading or trailing slash, no `.`/`..` or empty segments. The library root is `""`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

LIBRARY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
MAX_SEGMENT_BYTES = 255


class PathError(ValueError):
    """A path or folder reference is malformed."""


def normalize_relpath(path: str) -> str:
    """Normalize a library-relative path, or raise PathError."""
    if "\x00" in path:
        raise PathError("path contains a NUL character")
    path = unicodedata.normalize("NFC", path.replace("\\", "/")).strip("/")
    if not path:
        return ""
    segments = path.split("/")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise PathError(f"path {path!r} has an empty, '.' or '..' segment")
        if len(segment.encode()) > MAX_SEGMENT_BYTES:
            raise PathError(f"path segment {segment[:40]!r}… is longer than 255 bytes")
    return "/".join(segments)


def ancestors(relpath: str) -> list[str]:
    """The root and every ancestor of `relpath`, then `relpath` itself, outermost first.

    `ancestors("a/b/c") == ["", "a", "a/b", "a/b/c"]`
    """
    if not relpath:
        return [""]
    parts = relpath.split("/")
    return ["", *("/".join(parts[: i + 1]) for i in range(len(parts)))]


def is_within(relpath: str, parent: str) -> bool:
    """True if `relpath` is `parent` or inside it."""
    return parent == "" or relpath == parent or relpath.startswith(parent + "/")


def check_library_id(library_id: str) -> str:
    if not LIBRARY_ID_PATTERN.match(library_id):
        raise PathError(
            f"library id {library_id!r} must be lowercase letters, digits, '-' or '_', "
            "starting with a letter"
        )
    return library_id


@dataclass(frozen=True, slots=True, order=True)
class FolderRef:
    """A folder by location: `library:relative/path` (`library:` is the root)."""

    library_id: str
    relpath: str

    @classmethod
    def parse(cls, text: str) -> FolderRef:
        library_id, sep, relpath = text.partition(":")
        if not sep:
            raise PathError(f"folder reference {text!r} must look like 'library:path/to/folder'")
        return cls.of(library_id, relpath)

    @classmethod
    def of(cls, library_id: str, relpath: str) -> FolderRef:
        return cls(check_library_id(library_id), normalize_relpath(relpath))

    def __str__(self) -> str:
        return f"{self.library_id}:{self.relpath}"
