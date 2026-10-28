# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Who may call what: the guard every route goes through (docs/adr/0013).

`guard` is a dependency of the whole app, so it runs before every route, including
module pages. It authenticates the request (an `Authorization: Bearer stw_…` token, or
else the session cookie) and then applies the route's access level:

- `public`: anyone (login page, `/health`). Forms still need the pre-session CSRF cookie.
- `setup`: only while no owner exists, and only with the setup token's cookie.
- `session`: a logged-in browser session. **The default**, for every route that doesn't
  say otherwise, and the only level module pages can have.
- `principal`: a session or an API token (`/api/v1`); routes check token scopes.

Every POST/PUT/PATCH/DELETE from a session must carry the session's CSRF token (form
field `csrf_token` or header `X-CSRF-Token`), and cross-site requests that change
anything are refused outright (`Sec-Fetch-Site: cross-site`). Requests with an API
token need no CSRF token: browsers never attach one by themselves.
"""

from __future__ import annotations

import ipaddress
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from fastapi import Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData

from steward.core.auth import Auth, Principal, check_csrf, client_ip, csrf_token
from steward.core.auth.clientip import UNKNOWN, is_trusted_proxy
from steward.core.config import ApiConfig
from steward.core.setup import SetupToken

type Access = Literal["public", "setup", "session", "principal"]
F = TypeVar("F", bound=Callable[..., Any])

SESSION_COOKIE = "steward_session"
SETUP_COOKIE = "steward_setup"
PRESESSION_COOKIE = "steward_presession"
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MAX_FORM_FIELDS = 200

_LEVELS: dict[Callable[..., Any], Access] = {}
"""Access levels of core routes. Endpoints not in here (every module route) are `session`."""


def access(level: Access) -> Callable[[F], F]:
    """Mark a core route's access level (default `session`). Modules can't import this."""

    def mark(fn: F) -> F:
        _LEVELS[fn] = level
        return fn

    return mark


def level_of(endpoint: object) -> Access:
    return _LEVELS.get(endpoint, "session") if callable(endpoint) else "session"


# --- refusals (turned into responses by the handlers in app.py) ----------------------


class Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class NeedLogin(Exception):
    """No valid session or token."""


class SetupRequired(Exception):
    """No owner account yet: everything except setup is closed."""


class SetupClosed(Exception):
    """The owner exists, so the token-protected setup steps are over."""


# --- per-request facts -------------------------------------------------------------


@dataclass
class ApiState:
    auth: Auth
    api: ApiConfig
    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    setup_token: SetupToken | None
    owner_exists: bool = False
    """Cached once true (owners are never deleted)."""

    def has_owner(self) -> bool:
        if not self.owner_exists:
            self.owner_exists = self.auth.has_owner()
        return self.owner_exists


def api_state(request: Request) -> ApiState:
    state: ApiState = request.app.state.steward
    return state


def is_https(request: Request, state: ApiState) -> bool:
    if state.api.secure_cookies != "auto":
        return state.api.secure_cookies == "always"
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client else None
    if not is_trusted_proxy(peer, trusted=state.trusted_proxies):
        return False
    proto = request.headers.get("x-forwarded-proto", "").split(",")[-1].strip().lower()
    return proto == "https"


def presented_csrf(request: Request, form: FormData | None) -> str | None:
    header = request.headers.get(CSRF_HEADER)
    if header:
        return header
    value = form.get(CSRF_FIELD) if form is not None else None
    return value if isinstance(value, str) else None


async def _form(request: Request) -> FormData | None:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        return await request.form(max_fields=MAX_FORM_FIELDS)
    return None


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return ""  # present but unusable: refused, never falls back to the cookie
    return value.strip()


async def guard(request: Request) -> None:
    """The app-wide dependency. Sets `request.state.principal`, `.session_secret`,
    `.csrf`, `.presession`, `.ip` and `.https`, or raises a refusal."""
    state = api_state(request)
    level = level_of(request.scope.get("endpoint"))
    peer = request.client.host if request.client else None
    ip = client_ip(peer, request.headers.getlist("x-forwarded-for"), trusted=state.trusted_proxies)
    request.state.ip = ip if ip != UNKNOWN else (peer or UNKNOWN)
    request.state.https = is_https(request, state)
    request.state.principal = None
    request.state.session_secret = None
    request.state.csrf = None
    presession = request.cookies.get(PRESESSION_COOKIE)
    request.state.presession_new = not presession
    request.state.presession = presession or secrets.token_urlsafe(24)

    unsafe = request.method not in SAFE_METHODS
    if unsafe and request.headers.get("sec-fetch-site") == "cross-site":
        raise Refused(403, "cross-site requests can't change anything here")

    bearer = _bearer(request)
    principal: Principal | None = None
    if bearer is not None:
        principal = await run_in_threadpool(state.auth.authenticate_token, bearer, ip=ip)
        if principal is None and level != "public":
            raise NeedLogin
    else:
        secret = request.cookies.get(SESSION_COOKIE)
        if secret:
            principal = await run_in_threadpool(state.auth.authenticate_session, secret)
            if principal is not None:
                request.state.session_secret = secret
                request.state.csrf = csrf_token(secret)
    request.state.principal = principal

    form = await _form(request) if unsafe else None
    has_owner = await run_in_threadpool(state.has_owner)

    if level == "public":
        if unsafe and principal is None:
            _check_presession(request, form)
        elif unsafe and principal is not None and principal.is_session:
            _check_session_csrf(request, form)
        return
    if level == "setup":
        if has_owner:
            raise SetupClosed
        token = state.setup_token
        if token is None or not token.check(request.cookies.get(SETUP_COOKIE)):
            raise SetupRequired
        if unsafe:
            _check_presession(request, form)
        return
    if not has_owner:
        raise SetupRequired
    if principal is None:
        raise NeedLogin
    if level == "session" and not principal.is_session:
        raise Refused(403, "this page needs a logged-in session, not an API token")
    if unsafe and principal.is_session:
        _check_session_csrf(request, form)


def _check_session_csrf(request: Request, form: FormData | None) -> None:
    secret = request.state.session_secret
    if secret is None or not check_csrf(secret, presented_csrf(request, form)):
        raise Refused(403, "the form expired or came from somewhere else; reload and try again")


def _check_presession(request: Request, form: FormData | None) -> None:
    cookie = request.cookies.get(PRESESSION_COOKIE)
    presented = presented_csrf(request, form)
    if not cookie or not presented or not secrets.compare_digest(cookie, presented):
        raise Refused(403, "the form expired or cookies are blocked; reload and try again")


def require_principal(request: Request) -> Principal:
    principal: Principal | None = request.state.principal
    if principal is None:
        raise NeedLogin
    return principal


def safe_next(value: str | None, default: str = "/ui/") -> str:
    """Only same-site paths, so `?next=` can't send anyone elsewhere."""
    if not value or not value.startswith("/") or value.startswith(("//", "/\\")):
        return default
    if any(c in value for c in "\r\n\\") or ":" in value.split("?", 1)[0]:
        return default
    return value
