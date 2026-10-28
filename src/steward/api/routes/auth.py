# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Logging in and out, and re-entering the password before sensitive steps."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response

from steward.api.security import SESSION_COOKIE, access, api_state, require_principal, safe_next
from steward.api.templating import clear_cookie, redirect, render, session_max_age, set_cookie
from steward.core.auth import LockedOut, LoginFailed, NotAllowed

router = APIRouter()


def _locked(request: Request, template: str, exc: LockedOut, **context: object) -> Response:
    return render(
        request,
        template,
        {"error": str(exc), **context},
        status=429,
        headers={"Retry-After": str(exc.retry_after)},
    )


@router.get("/login")
@access("public")
def login_page(request: Request, next: str | None = None) -> Response:
    if not api_state(request).has_owner():
        return redirect("/setup")
    principal = request.state.principal
    if principal is not None and principal.is_session:
        return redirect(safe_next(next))
    return render(request, "login.html", {"next": safe_next(next)})


@router.post("/login")
@access("public")
def login(
    request: Request,
    username: Annotated[str, Form(max_length=256)],
    password: Annotated[str, Form(max_length=4096)],
    next: Annotated[str, Form(max_length=2048)] = "/ui/",
) -> Response:
    state = api_state(request)
    if not state.has_owner():
        return redirect("/setup")
    target = safe_next(next)
    context = {"next": target, "username": username}
    try:
        session = state.auth.login(
            username, password, ip=request.state.ip, user_agent=request.headers.get("user-agent")
        )
    except LockedOut as exc:
        return _locked(request, "login.html", exc, **context)
    except LoginFailed as exc:
        return render(request, "login.html", {"error": str(exc), **context}, status=401)
    response = redirect(target)
    set_cookie(request, response, SESSION_COOKIE, session.secret, max_age=session_max_age(request))
    return response


@router.post("/logout")
def logout(request: Request) -> Response:
    state = api_state(request)
    secret = request.state.session_secret
    if secret:
        state.auth.logout(secret)
    response = redirect("/login")
    clear_cookie(request, response, SESSION_COOKIE)
    return response


@router.get("/reauth")
def reauth_page(request: Request, next: str | None = None) -> Response:
    return render(request, "reauth.html", {"next": safe_next(next)})


@router.post("/reauth")
def reauth(
    request: Request,
    password: Annotated[str, Form(max_length=4096)],
    next: Annotated[str, Form(max_length=2048)] = "/ui/",
) -> Response:
    principal = require_principal(request)
    target = safe_next(next)
    try:
        api_state(request).auth.reauth(principal, password, ip=request.state.ip)
    except LockedOut as exc:
        return _locked(request, "reauth.html", exc, next=target)
    except (LoginFailed, NotAllowed) as exc:
        return render(request, "reauth.html", {"error": str(exc), "next": target}, status=401)
    return redirect(target)
