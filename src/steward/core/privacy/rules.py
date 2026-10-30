# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Path and name rules from `privacy.yaml`: secret patterns and sensitivity rules.

Pattern syntax (matched case-insensitively against a library-relative path):

- No `/`: matches a file or folder **name** anywhere (`*.kdbx`, `id_rsa*`, `.env`).
  A folder name match covers everything inside that folder.
- With `/`: matched against the path, segment by segment, at any depth (`.ssh/**`,
  `taxes/*.pdf`); `**` matches any number of segments, `*`/`?`/`[...]` work inside one
  segment. A leading `/` anchors the pattern at the library root (`/private/**`).
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import lru_cache

from steward.core.config import PrivacyConfig
from steward.core.folders.types import Sensitivity, max_sensitivity


def matches(relpath: str, pattern: str) -> bool:
    """True if `relpath` (or a folder containing it) matches `pattern`."""
    path = relpath.casefold().split("/") if relpath else []
    pat = pattern.casefold().strip()
    if "/" not in pat.strip("/"):
        name = pat.strip("/")
        return any(fnmatchcase(segment, name) for segment in path)
    anchored = pat.startswith("/")
    segments = _segments(pat.strip("/"))
    starts = [0] if anchored else range(len(path) + 1)
    return any(_match(path[i:], segments) for i in starts)


@lru_cache(maxsize=512)
def _segments(pattern: str) -> tuple[str, ...]:
    return tuple(s for s in pattern.split("/") if s)


def _match(path: list[str], pattern: tuple[str, ...]) -> bool:
    """Match `pattern` against a prefix of `path` that ends at a segment boundary; the
    remaining path (if any) is inside a matched folder. A trailing `**` matches the
    folder's contents but not the folder itself."""
    if not pattern:
        return True
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        if not rest:
            return len(path) > 0
        return any(_match(path[i:], rest) for i in range(len(path) + 1))
    if not path or not fnmatchcase(path[0], head):
        return False
    return _match(path[1:], rest)


@dataclass(frozen=True)
class PrivacyRules:
    """Compiled `privacy.yaml`."""

    secret_patterns: tuple[str, ...]
    rules: tuple[tuple[str, Sensitivity], ...] = ()
    content_scan: bool = True
    content_scan_ignore: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, config: PrivacyConfig) -> PrivacyRules:
        return cls(
            secret_patterns=(*config.secret_patterns, *config.extra_secret_patterns),
            rules=tuple((r.pattern, r.sensitivity) for r in config.rules),
            content_scan=config.content_scan,
            content_scan_ignore=config.content_scan_ignore,
        )

    @classmethod
    def default(cls) -> PrivacyRules:
        return cls.from_config(PrivacyConfig(config_version=1))

    def scans_content(self, relpath: str) -> bool:
        """Whether text from `relpath` is looked through for credentials."""
        return self.content_scan and not any(matches(relpath, p) for p in self.content_scan_ignore)

    def is_secret(self, relpath: str) -> bool:
        return any(matches(relpath, p) for p in self.secret_patterns)

    def level_for(self, relpath: str) -> Sensitivity:
        """The highest level any rule gives `relpath` (`normal` if none matches)."""
        if self.is_secret(relpath):
            return "excluded"
        level: Sensitivity = "normal"
        for pattern, rule_level in self.rules:
            if matches(relpath, pattern):
                level = max_sensitivity(level, rule_level)
        return level
