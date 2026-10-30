# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Finding credentials in a file's text (docs/adr/0029).

The patterns in `privacy.yaml` go by a file's name, so a password inside an ordinary
config file or note isn't caught by them. `find_secret` looks at the text itself: private
keys, tokens with a recognizable prefix, passwords in a URL, and `name: value` lines where
the name says it's a credential and the value looks like a real one.

It prefers missing a weak password to flagging every config template: placeholders
(`${DB_PASSWORD}`, `<token>`, `changeme`), references (`self.password`) and plain words
aren't findings. It returns the rule's name and never the value: that goes in events and
job results, which must not hold file content.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

VERSION = 1


class SecretFound(Exception):
    """Text about to be stored holds a credential. `rule` names the kind, never the value."""

    def __init__(self, rule: str) -> None:
        super().__init__(rule)
        self.rule = rule


_TOKENS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ ){0,3}PRIVATE KEY(?: BLOCK)?-----")),
    ("AWS key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("Slack token", re.compile(r"\bxox[abeoprs]-[A-Za-z0-9-]{20,}")),
    ("Stripe key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{20,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("model API key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}")),
    ("steward-ai token", re.compile(r"\bstw_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])")),
)

# A name ending in a credential word, then `:` or `=`, then the value (quotes optional).
_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?<![a-z0-9])
    (?:[a-z0-9]{1,24}[_.-]){0,4}
    (?P<name>
        password | passwd | passphrase | pwd
      | secret | secret[_-]?key | client[_-]?secret | private[_-]?key
      | api[_-]?key | access[_-]?key | auth[_-]?key
      | token | auth[_-]?token | access[_-]?token | refresh[_-]?token | bearer
    )
    ["']? \s{0,4} (?::=|=>|=|:) \s{0,4} ["'`]?
    (?P<value>[^\s"'`,;(){}<>\[\]\\]{8,256})
    """
)

_URL_PASSWORD = re.compile(
    r"\b[a-z][a-z0-9+.-]{1,20}://[^\s:/@\"']{1,64}:(?P<value>[^\s/@\"']{6,128})@[^\s/\"']",
    re.IGNORECASE,
)

_PASSWORD_NAMES = frozenset({"password", "passwd", "passphrase", "pwd"})

_REFERENCE = re.compile(r"^[$%&*@!#~]|^env[.:]|^vault:|\.\.\.|^https?:", re.IGNORECASE)
_PLAIN_NAME = re.compile(r"^[A-Za-z_.:/-]+$")
_PLACEHOLDER = re.compile(
    r"change[_-]?(?:me|it|this)|example|sample|placeholder|dummy|fake|redacted|replace|"
    r"your|xxxx|\*\*\*|0000000|12345678|abcdefgh|password|passw0rd|p@ssw|secret|"
    r"insert|generate|todo|fixme|test|default",
    re.IGNORECASE,
)
_HAS_DIGIT = re.compile(r"\d")
_HAS_LETTER = re.compile(r"[A-Za-z]")


def _credible(name: str, value: str) -> bool:
    """Whether `value` looks like a real credential rather than a template or code."""
    if _REFERENCE.search(value) or _PLACEHOLDER.search(value):
        return False
    if len(set(value)) < 5:
        return False
    if _PLAIN_NAME.match(value) and "-" not in value:
        return False  # a name, a path or a plain word: `self.password`, `Authorization`
    if name.lower() in _PASSWORD_NAMES:
        return True
    return (
        len(value) >= 16
        and _HAS_DIGIT.search(value) is not None
        and _HAS_LETTER.search(value) is not None
    )


def find_secret(texts: Iterable[str]) -> str | None:
    """The name of the first kind of credential found in `texts`, else None."""
    for text in texts:
        for name, pattern in _TOKENS:
            if pattern.search(text):
                return name
        for match in _URL_PASSWORD.finditer(text):
            if _credible("password", match["value"]):
                return "password in a URL"
        for match in _ASSIGNMENT.finditer(text):
            if _credible(match["name"], match["value"]):
                return f"{match['name'].lower()} value"
    return None
