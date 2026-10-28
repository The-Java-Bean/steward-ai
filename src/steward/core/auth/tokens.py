# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Random secrets for sessions and API tokens, and CSRF tokens derived from sessions.

Secrets are 256-bit random values. The database only stores their SHA-256 (a fast hash
is enough: the secrets are random, not chosen by people, so they can't be guessed from
a list). API tokens start with `stw_` so secret scanners and people can recognize them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

SECRET_BYTES = 32
API_TOKEN_PREFIX = "stw_"  # noqa: S105 -- a prefix, not a secret
HINT_LENGTH = 4
_CSRF_CONTEXT = b"steward-ai csrf v1"


def new_session_secret() -> str:
    return secrets.token_urlsafe(SECRET_BYTES)


def new_api_token() -> str:
    return API_TOKEN_PREFIX + secrets.token_urlsafe(SECRET_BYTES)


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def hint(token: str) -> str:
    """The last few characters, shown in token lists (`stw_…a1b2`)."""
    return token[-HINT_LENGTH:]


def looks_like_api_token(value: str) -> bool:
    return value.startswith(API_TOKEN_PREFIX) and len(value) > len(API_TOKEN_PREFIX) + 32


def csrf_token(session_secret: str) -> str:
    """The CSRF token for a session: an HMAC of the session secret, so nothing extra is
    stored and it changes whenever the session does."""
    mac = hmac.new(session_secret.encode("utf-8"), _CSRF_CONTEXT, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def check_csrf(session_secret: str, presented: str | None) -> bool:
    if not presented:
        return False
    return hmac.compare_digest(csrf_token(session_secret), presented)
