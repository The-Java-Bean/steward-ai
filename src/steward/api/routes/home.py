# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The home page, `/health` and `/api/v1/me`."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from steward.api.routes.setup import next_page, progress_of
from steward.api.security import access, api_state, require_principal
from steward.api.templating import redirect, render

router = APIRouter()


@router.get("/")
def root(request: Request) -> Response:
    return redirect("/ui/")


@router.get("/ui/")
def home(request: Request) -> Response:
    target = next_page(request)
    if target != "/ui/":
        return redirect(target)
    return render(
        request,
        "home.html",
        {"progress": progress_of(request), "login_settings": api_state(request).auth.settings()},
    )


@router.get("/health")
@access("public")
def health() -> Response:
    """Liveness for container health checks. Says nothing else; details are on the
    health page, behind login."""
    return JSONResponse({"status": "ok"})


@router.get("/api/v1/me")
@access("principal")
def me(request: Request) -> Response:
    principal = require_principal(request)
    return JSONResponse(
        {
            "username": principal.user.username,
            "role": principal.user.role,
            "via": principal.via,
            "scopes": sorted(principal.scopes) if not principal.is_session else ["*"],
        }
    )
