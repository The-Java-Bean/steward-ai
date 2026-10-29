# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Share tokens, share passwords (argon2id) and the cookie that remembers a correct
password for a while."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD = 8
MAX_PASSWORD = 1024
GRANT_COOKIE = "steward_share"
GRANT_LIFETIME = timedelta(hours=12)

_hasher = PasswordHasher()
"""argon2id with the library's defaults; tests swap in cheap parameters."""
_hashing = threading.BoundedSemaphore(2)
"""At most two hashes at a time (each takes tens of MiB), however many visitors try."""


def new_token() -> str:
    """128 random bits, 22 URL-safe characters (`steward.sdk.TOKEN_PATTERN`)."""
    return secrets.token_urlsafe(16)


def new_share_id() -> str:
    return secrets.token_hex(16)


def suggest_password() -> str:
    """A password to offer in the share dialog: easy to read out, ~80 bits."""
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    groups = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4)]
    return "-".join(groups)


def hash_password(password: str) -> str:
    with _hashing:
        return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    if not password or len(password) > MAX_PASSWORD:
        return False
    with _hashing:
        try:
            return _hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False


def _signature(password_hash: str, share_id: str, expires: int) -> str:
    message = f"share-grant:{share_id}:{expires}".encode()
    return hmac.new(password_hash.encode(), message, hashlib.sha256).hexdigest()


def make_grant(password_hash: str, share_id: str, now: datetime) -> str:
    """Cookie value proving the password was given. Keyed by the password hash, so a
    new password ends every grant."""
    expires = int((now + GRANT_LIFETIME).timestamp())
    return f"{expires}.{_signature(password_hash, share_id, expires)}"


def check_grant(password_hash: str, share_id: str, value: str | None, now: datetime) -> bool:
    if not value:
        return False
    expires_text, _, signature = value.partition(".")
    if not expires_text.isdigit() or int(expires_text) <= now.timestamp():
        return False
    expected = _signature(password_hash, share_id, int(expires_text))
    return hmac.compare_digest(expected, signature)
