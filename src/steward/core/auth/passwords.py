# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Password hashing (argon2id) and the password rules.

Passwords are NFKC-normalized before hashing, so the same password typed on different
keyboards or systems matches. At most `MAX_CONCURRENT_HASHES` hashes run at once:
each one takes tens of milliseconds and 64 MiB, and a burst of login requests must not
exhaust the server's memory.
"""

from __future__ import annotations

import threading
import unicodedata
from functools import cache
from importlib import resources

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 12
MAX_LENGTH = 1024
MIN_DISTINCT_CHARACTERS = 4
MAX_CONCURRENT_HASHES = 2

# argon2-cffi's defaults are RFC 9106's low-memory profile: argon2id, t=3, m=64 MiB, p=4.
_hasher = PasswordHasher()
_slots = threading.BoundedSemaphore(MAX_CONCURRENT_HASHES)
_TRAILING = "0123456789!@#$%^&*()-_=+.?,;:~ "


class WeakPasswordError(ValueError):
    """The password doesn't meet the rules; the message says why (never repeats it)."""


def normalize(password: str) -> str:
    return unicodedata.normalize("NFKC", password)


def hash_password(password: str) -> str:
    with _slots:
        return _hasher.hash(normalize(password))


def verify_password(stored_hash: str, password: str) -> bool:
    """Constant-time check. A malformed stored hash counts as a mismatch."""
    if len(password) > MAX_LENGTH:
        return False
    with _slots:
        try:
            return _hasher.verify(stored_hash, normalize(password))
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False


def needs_rehash(stored_hash: str) -> bool:
    """True when the hash was made with older parameters; rehash after a good login."""
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def dummy_verify(password: str) -> None:
    """Spend the same time as a real check, for unknown usernames (no timing oracle)."""
    verify_password(_dummy_hash(), password)


@cache
def _dummy_hash() -> str:
    return hash_password("steward-ai dummy password, never valid")


@cache
def common_passwords() -> frozenset[str]:
    text = resources.files(__package__).joinpath("common_passwords.txt").read_text("utf-8")
    return frozenset(
        line.strip().lower() for line in text.splitlines() if line.strip() and line[0] != "#"
    )


@cache
def _common_stems() -> frozenset[str]:
    """The list's entries without trailing digits and symbols (`password1234` →
    `password`), so `password!!2026` is refused too."""
    return frozenset(stem for p in common_passwords() if (stem := p.rstrip(_TRAILING)))


def problems(password: str, *, username: str | None = None) -> list[str]:
    """Every rule the password breaks, as messages for the user (empty = acceptable)."""
    found: list[str] = []
    normalized = normalize(password)
    if len(normalized) < MIN_LENGTH:
        found.append(f"must be at least {MIN_LENGTH} characters long")
    if len(normalized) > MAX_LENGTH:
        found.append(f"must be at most {MAX_LENGTH} characters long")
    folded = normalized.casefold()
    if len(set(folded)) < MIN_DISTINCT_CHARACTERS:
        found.append(f"must use at least {MIN_DISTINCT_CHARACTERS} different characters")
    if folded in common_passwords() or folded.rstrip(_TRAILING) in _common_stems():
        found.append("is too common; choose something less predictable")
    if username:
        name = normalize(username).casefold()
        if len(name) >= 3 and name in folded:
            found.append("must not contain the username")
    return found


def check(password: str, *, username: str | None = None) -> None:
    """Raise `WeakPasswordError` if the password breaks a rule."""
    found = problems(password, username=username)
    if found:
        raise WeakPasswordError("password " + "; ".join(found))
