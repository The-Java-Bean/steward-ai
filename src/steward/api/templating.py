# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Rendering pages and setting cookies."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PrefixLoader, select_autoescape

import steward
from steward.api.security import PRESESSION_COOKIE, api_state
from steward.sdk import Badge
from steward.ui import TEMPLATES_DIR

log = logging.getLogger(__name__)

templates = Jinja2Templates(directory=TEMPLATES_DIR)
"""The core's own templates (used when an app has no `state.templates`)."""


def make_templates(module_dirs: Mapping[str, Path]) -> Jinja2Templates:
    """Core templates plus each module's, named `<module>/<template>` (they may extend
    the core's `base.html`)."""
    loader = ChoiceLoader(
        [
            FileSystemLoader(TEMPLATES_DIR),
            PrefixLoader({m: FileSystemLoader(d) for m, d in module_dirs.items()}, delimiter="/"),
        ]
    )
    env = Environment(loader=loader, autoescape=select_autoescape(default=True), auto_reload=False)
    return Jinja2Templates(env=env)


NOTICES = {
    "password_changed": "Your password was changed. Your other sessions were logged out.",
    "session_revoked": "The session was logged out.",
    "sessions_revoked": "Your other sessions were logged out.",
    "settings_saved": "Login settings saved.",
    "token_revoked": "The API token was revoked.",
    "setup_finished": "Setup is finished.",
    "reauth_ok": "Password confirmed.",
    "undone": "The action was undone.",
    "job_retried": "The job was queued again.",
    "job_not_dead": "Only jobs that gave up can be retried.",
    "folder_saved": "Folder settings saved.",
    "folder_locked": "The folder is locked.",
    "folder_unlocked": "The folder is unlocked.",
}
"""Fixed messages shown after a redirect (`?notice=<key>`), so no text is reflected."""


@dataclass(frozen=True, slots=True)
class NavEntry:
    path: str
    title: str
    badge: Badge | None = None


@dataclass(frozen=True, slots=True)
class NavItem:
    path: str
    title: str
    count: int = 0


def nav(request: Request) -> list[NavItem]:
    entries: list[NavEntry] = request.app.state.nav
    items: list[NavItem] = []
    for entry in entries:
        count = 0
        if entry.badge is not None:
            try:
                count = int(entry.badge(request.app.state.runtime.dbs.state))
            except Exception:  # a module's bug mustn't break every page
                log.warning("navigation badge failed", extra={"fields": {"page": entry.path}})
        items.append(NavItem(entry.path, entry.title, count))
    return items


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    principal = getattr(request.state, "principal", None)
    notice = NOTICES.get(request.query_params.get("notice", ""))
    ctx: dict[str, Any] = {
        "principal": principal,
        "csrf": getattr(request.state, "csrf", None),
        "presession": getattr(request.state, "presession", None),
        "form_token": getattr(request.state, "csrf", None)
        or getattr(request.state, "presession", None),
        "nav": nav(request) if principal is not None and principal.is_session else [],
        "version": steward.__version__,
        "notice": notice,
        "path": request.url.path,
        **(context or {}),
    }
    found: Jinja2Templates = getattr(request.app.state, "templates", templates)
    response = found.TemplateResponse(request, name, ctx, status_code=status, headers=headers)
    if getattr(request.state, "presession_new", False):
        set_cookie(request, response, PRESESSION_COOKIE, request.state.presession, max_age=None)
    return response


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def set_cookie(
    request: Request, response: Response, name: str, value: str, *, max_age: int | None
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/",
        httponly=True,
        secure=bool(getattr(request.state, "https", False)),
        samesite="strict",
    )


def clear_cookie(request: Request, response: Response, name: str) -> None:
    response.delete_cookie(
        name,
        path="/",
        httponly=True,
        secure=bool(getattr(request.state, "https", False)),
        samesite="strict",
    )


def session_max_age(request: Request) -> int:
    return int(api_state(request).auth.max_age.total_seconds())
