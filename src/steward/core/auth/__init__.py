# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Login, sessions, re-auth, API tokens and rate limiting (docs/adr/0012)."""

from steward.core.auth.clientip import client_ip
from steward.core.auth.limits import Lockout
from steward.core.auth.passwords import WeakPasswordError
from steward.core.auth.service import (
    TOKEN_SCOPES,
    AccountError,
    Auth,
    AuthError,
    AuthSettings,
    LockedOut,
    LoginFailed,
    NewSession,
    NotAllowed,
    Principal,
    ReauthRequired,
    SessionInfo,
    TokenInfo,
    TokenScope,
    User,
    normalize_username,
)
from steward.core.auth.tokens import check_csrf, csrf_token

__all__ = [
    "TOKEN_SCOPES",
    "AccountError",
    "Auth",
    "AuthError",
    "AuthSettings",
    "LockedOut",
    "Lockout",
    "LoginFailed",
    "NewSession",
    "NotAllowed",
    "Principal",
    "ReauthRequired",
    "SessionInfo",
    "TokenInfo",
    "TokenScope",
    "User",
    "WeakPasswordError",
    "check_csrf",
    "client_ip",
    "csrf_token",
    "normalize_username",
]
