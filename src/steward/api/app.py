# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The FastAPI application: core routes, module pages, refusals, security headers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Mount

import steward
from steward.api.routes import api_v1
from steward.api.routes import auth as auth_routes
from steward.api.routes import home as home_routes
from steward.api.routes import pages as page_routes
from steward.api.routes import settings as settings_routes
from steward.api.routes import setup as setup_routes
from steward.api.routes import shares as share_routes
from steward.api.routes import uploads as upload_routes
from steward.api.security import (
    ApiState,
    NeedLogin,
    Refused,
    SetupClosed,
    SetupRequired,
    guard,
    safe_next,
)
from steward.api.templating import NavEntry, make_templates, redirect, render
from steward.api.web import PageInfo, make_web
from steward.core.auth import AuthError, LockedOut, NotAllowed, ReauthRequired
from steward.core.loader import ModuleLoadError
from steward.core.runtime import Runtime
from steward.core.setup import SetupToken
from steward.sdk import ShareLinkHandler
from steward.sdk.web import NotFound
from steward.ui import STATIC_DIR

CORE_UI_PATHS = frozenset(
    {
        "/ui/settings",
        "/ui/jobs",
        "/ui/health",
        "/ui/proposals",
        "/ui/journal",
        "/ui/setup",
        "/ui/search",
    }
)
"""Paths the core uses or will use; module pages can't take them."""

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; style-src 'self'; img-src 'self' data:; media-src 'self'; "
    "frame-src 'self'; script-src 'self'; connect-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)
MODULE_STATIC = "/static/m"
"""Module static files are served at `/static/m/<module>/`."""


class UiPageError(ModuleLoadError):
    """A module's UI page can't be mounted (reserved or duplicate path, unsafe route)."""


def create_app(runtime: Runtime, *, setup_token: SetupToken | None = None) -> FastAPI:
    app = FastAPI(
        title="steward-ai",
        version=steward.__version__,
        dependencies=[Depends(guard)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runtime = runtime
    app.state.steward = ApiState(
        auth=runtime.auth,
        api=runtime.config.steward.api,
        trusted_proxies=runtime.config.steward.trusted_proxies,
        setup_token=setup_token,
    )
    nav = [NavEntry("/ui/", "Home"), NavEntry("/ui/search", "Search")]
    for router in (
        home_routes.router,
        auth_routes.router,
        setup_routes.router,
        settings_routes.router,
        page_routes.router,
        api_v1.router,
        upload_routes.router,
        share_routes.router,
    ):
        app.include_router(router)
    nav.extend(_mount_module_pages(app, runtime))
    nav.extend(
        [
            NavEntry("/ui/journal", "Journal"),
            NavEntry("/ui/jobs", "Jobs"),
            NavEntry("/ui/health", "Health"),
            NavEntry("/ui/settings", "Settings"),
        ]
    )
    app.state.nav = nav
    app.state.steward_web = make_web
    app.state.link_builders = list(runtime.registrations.link_builders().values())
    app.state.share_links = _share_links(runtime)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    _check_routes(app)
    _add_handlers(app)
    app.middleware("http")(_security_headers)
    return app


def _share_links(runtime: Runtime) -> tuple[str, ShareLinkHandler] | None:
    handlers = runtime.registrations.share_links()
    if len(handlers) > 1:
        raise UiPageError(
            "only one module may answer share links; enabled: " + ", ".join(sorted(handlers))
        )
    return next(iter(handlers.items()), None)


def _mount_module_pages(app: FastAPI, runtime: Runtime) -> list[NavEntry]:
    entries: list[NavEntry] = []
    used: dict[str, str] = {}
    templates: dict[str, Path] = {}
    static: dict[str, Path] = {}
    pages: list[PageInfo] = []
    for name, page in runtime.registrations.ui_pages().items():
        module = runtime.registrations.module_of("ui_page", name)
        if page.path in CORE_UI_PATHS:
            raise UiPageError(f"UI page {name!r}: {page.path} is reserved for the core")
        if page.path in used:
            raise UiPageError(
                f"UI page {name!r}: {page.path} is already used by UI page {used[page.path]!r}"
            )
        for context in iter_route_contexts(page.router.routes):
            route = context.original_route
            if not isinstance(route, APIRoute):
                raise UiPageError(
                    f"UI page {name!r}: only regular HTTP routes are allowed "
                    f"(found {type(route).__name__})"
                )
        for label, found, directory in (
            ("templates", templates, page.templates),
            ("static", static, page.static),
        ):
            if directory is None:
                continue
            if found.setdefault(module, directory) != directory:
                raise UiPageError(f"UI page {name!r}: module {module!r} has two {label} folders")
        used[page.path] = name
        app.include_router(page.router, prefix=page.path)
        pages.append(PageInfo(module, page.path, False, False))
        if page.nav:
            entries.append(NavEntry(page.path, page.title, page.badge))
    app.state.page_paths = {
        name: page.path for name, page in runtime.registrations.ui_pages().items()
    }
    app.state.module_pages = sorted(
        (PageInfo(p.module, p.path, p.module in templates, p.module in static) for p in pages),
        key=lambda p: len(p.path),
        reverse=True,
    )
    app.state.templates = make_templates(templates)
    for module, directory in static.items():
        app.mount(f"{MODULE_STATIC}/{module}", StaticFiles(directory=directory))
    return entries


def _check_routes(app: FastAPI) -> None:
    """Fail closed: every route must be one the guard dependency runs for."""
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if isinstance(route, APIRoute):
            continue
        if isinstance(route, Mount) and (
            route.path == "/static" or route.path.startswith(MODULE_STATIC + "/")
        ):
            continue
        raise UiPageError(f"route {getattr(route, 'path', route)!r} would bypass the login check")


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def _error(request: Request, status: int, message: str, **headers: str) -> Response:
    if _is_api(request):
        return JSONResponse({"error": message}, status_code=status, headers=headers or None)
    return render(
        request,
        "error.html",
        {"message": message, "status": status},
        status=status,
        headers=headers or None,
    )


def _back(request: Request) -> str:
    """Where to return after a re-auth: this page for GETs, the referring page for forms."""
    if request.method == "GET":
        query = f"?{request.url.query}" if request.url.query else ""
        return safe_next(request.url.path + query)
    referer = request.headers.get("referer", "")
    base = str(request.base_url).rstrip("/")
    if referer.startswith(base + "/"):
        return safe_next(referer[len(base) :])
    return "/ui/"


def _add_handlers(app: FastAPI) -> None:
    async def need_login(request: Request, exc: Exception) -> Response:
        if _is_api(request):
            return JSONResponse(
                {"error": "log in or send an API token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        target = safe_next(request.url.path if request.method == "GET" else None)
        return redirect(f"/login?next={quote(target)}")

    async def setup_required(request: Request, exc: Exception) -> Response:
        if _is_api(request):
            return JSONResponse({"error": "setup isn't finished"}, status_code=503)
        return redirect("/setup")

    async def setup_closed(request: Request, exc: Exception) -> Response:
        return redirect("/login")

    async def refused(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, Refused)  # noqa: S101 -- registered for Refused only
        return _error(request, exc.status, exc.message)

    async def reauth_required(request: Request, exc: Exception) -> Response:
        if _is_api(request):
            return _error(request, 403, str(exc))
        return redirect(f"/reauth?next={quote(_back(request))}")

    async def not_allowed(request: Request, exc: Exception) -> Response:
        return _error(request, 403, str(exc))

    async def locked_out(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, LockedOut)  # noqa: S101
        return _error(request, 429, str(exc), **{"Retry-After": str(exc.retry_after)})

    async def auth_error(request: Request, exc: Exception) -> Response:
        return _error(request, 400, str(exc))

    async def http_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, StarletteHTTPException)  # noqa: S101
        message = {404: "Not found.", 405: "Method not allowed."}.get(exc.status_code, "Error.")
        return _error(request, exc.status_code, message)

    async def invalid(request: Request, exc: Exception) -> Response:
        return _error(request, 400, "Some fields are missing or invalid.")

    async def not_found(request: Request, exc: Exception) -> Response:
        return _error(request, 404, "Not found.")

    app.add_exception_handler(NeedLogin, need_login)
    app.add_exception_handler(SetupRequired, setup_required)
    app.add_exception_handler(SetupClosed, setup_closed)
    app.add_exception_handler(Refused, refused)
    app.add_exception_handler(ReauthRequired, reauth_required)
    app.add_exception_handler(NotAllowed, not_allowed)
    app.add_exception_handler(LockedOut, locked_out)
    app.add_exception_handler(AuthError, auth_error)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, invalid)
    app.add_exception_handler(NotFound, not_found)


async def _security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    headers = response.headers
    headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "same-origin")
    headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if not request.url.path.startswith("/static/"):
        headers.setdefault("Cache-Control", "no-store")
    return response
