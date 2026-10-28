# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`/ui/settings`: account and password, sessions, the re-auth window, API tokens.

Folder types, libraries, models and modules join this page later. Steps that need a
recent password check raise `ReauthRequired`, which sends the browser to `/reauth`
and back here.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response

from steward.api.security import api_state, require_principal
from steward.api.templating import redirect, render
from steward.core.auth import TOKEN_SCOPES, AccountError, LockedOut, LoginFailed, WeakPasswordError
from steward.core.db import utcnow

router = APIRouter(prefix="/ui/settings")
MAX_TOKEN_DAYS = 3650


def _page(
    request: Request,
    *,
    error: str | None = None,
    new_token: str | None = None,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    auth = api_state(request).auth
    principal = require_principal(request)
    user_id = principal.user.user_id
    context: dict[str, Any] = {
        "user": principal.user,
        "current_session": principal.session_id,
        "sessions": auth.list_sessions(user_id),
        "login_settings": auth.settings(),
        "recent": auth.is_recent(principal),
        "tokens": [t for t in auth.list_tokens(user_id) if t.revoked_at is None],
        "scopes": TOKEN_SCOPES,
        "now": utcnow(),
        "error": error,
        "new_token": new_token,
    }
    return render(request, "settings.html", context, status=status, headers=headers)


@router.get("")
def settings_page(request: Request) -> Response:
    return _page(request)


@router.post("/password")
def change_password(
    request: Request,
    current: Annotated[str, Form(max_length=4096)],
    new: Annotated[str, Form(max_length=4096)],
    confirm: Annotated[str, Form(max_length=4096)],
) -> Response:
    principal = require_principal(request)
    if new != confirm:
        return _page(request, error="The two new passwords don't match.", status=400)
    try:
        api_state(request).auth.change_password(principal, current, new, ip=request.state.ip)
    except LockedOut as exc:
        return _page(
            request, error=str(exc), status=429, headers={"Retry-After": str(exc.retry_after)}
        )
    except LoginFailed:
        return _page(request, error="Your current password is wrong.", status=400)
    except WeakPasswordError as exc:
        return _page(request, error=str(exc), status=400)
    return redirect("/ui/settings?notice=password_changed")


@router.post("/sessions/revoke")
def revoke_session(request: Request, session_id: Annotated[int, Form()]) -> Response:
    principal = require_principal(request)
    auth = api_state(request).auth
    if session_id not in {s.session_id for s in auth.list_sessions(principal.user.user_id)}:
        return _page(request, error="No such session.", status=404)
    auth.revoke_session(session_id, actor=principal.actor, reason="logged out from settings")
    if session_id == principal.session_id:
        return redirect("/login")
    return redirect("/ui/settings?notice=session_revoked")


@router.post("/sessions/revoke-others")
def revoke_other_sessions(request: Request) -> Response:
    principal = require_principal(request)
    auth = api_state(request).auth
    for s in auth.list_sessions(principal.user.user_id):
        if s.session_id != principal.session_id:
            auth.revoke_session(s.session_id, actor=principal.actor, reason="logged out others")
    return redirect("/ui/settings?notice=sessions_revoked")


@router.post("/login")
def login_settings(
    request: Request,
    reauth_minutes: Annotated[int, Form(ge=0, le=24 * 60)],
    reauth_enabled: Annotated[bool, Form()] = False,
) -> Response:
    principal = require_principal(request)
    try:
        api_state(request).auth.update_settings(
            principal, {"reauth_enabled": reauth_enabled, "reauth_minutes": reauth_minutes}
        )
    except AccountError as exc:
        return _page(request, error=str(exc), status=400)
    return redirect("/ui/settings?notice=settings_saved")


@router.post("/tokens")
def create_token(
    request: Request,
    label: Annotated[str, Form(max_length=200)],
    scopes: Annotated[list[str] | None, Form()] = None,
    expires_days: Annotated[str, Form(max_length=10)] = "",
) -> Response:
    principal = require_principal(request)
    expires_at = None
    if expires_days.strip():
        try:
            days = int(expires_days)
        except ValueError:
            return _page(request, error="Expiry must be a number of days.", status=400)
        if not 1 <= days <= MAX_TOKEN_DAYS:
            return _page(
                request, error=f"Expiry must be 1-{MAX_TOKEN_DAYS} days, or empty.", status=400
            )
        expires_at = utcnow() + timedelta(days=days)
    try:
        _, secret = api_state(request).auth.create_token(
            principal, label, scopes or [], expires_at=expires_at
        )
    except AccountError as exc:
        return _page(request, error=str(exc), status=400)
    return _page(request, new_token=secret)


@router.post("/tokens/revoke")
def revoke_token(request: Request, token_id: Annotated[int, Form()]) -> Response:
    principal = require_principal(request)
    auth = api_state(request).auth
    if token_id not in {t.token_id for t in auth.list_tokens(principal.user.user_id)}:
        return _page(request, error="No such token.", status=404)
    auth.revoke_token(token_id, actor=principal.actor)
    return redirect("/ui/settings?notice=token_revoked")
