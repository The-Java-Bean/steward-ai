# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Every route refuses requests without a session or token (plan §17, "Security"),
every state-changing route refuses a session without its CSRF token, and API tokens
can't use browser-only routes. Routes are enumerated from the app, so new routes
(including module pages) are covered without editing this file."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from api_support import SETUP_TOKEN, USERNAME, log_in, make_owner
from fastapi import APIRouter, FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from starlette.responses import Response
from steward_hello import HelloModule
from support import make_module, make_runtime, use_fast_hashes

from steward.api import create_app
from steward.api.security import SAFE_METHODS, level_of
from steward.core.runtime import Runtime
from steward.core.setup import SetupToken
from steward.modules.files_ui import FilesUiModule
from steward.sdk import Registry

PUBLIC = {
    ("GET", "/health"),
    ("GET", "/login"),
    ("POST", "/login"),
    ("GET", "/setup"),
    ("POST", "/setup"),
}
"""The only routes anyone may call without logging in. Changing this set is a security
decision: say why in the commit."""
SETUP_ONLY = {("GET", "/setup/owner"), ("POST", "/setup/owner")}
TOKEN_OK = {
    ("GET", f"/api/v1/{path}")
    for path in (
        "me",
        "libraries",
        "folders",
        "files",
        "files/1",
        "search",
        "actions",
        "journal",
        "jobs",
        "health",
        "openapi.json",
    )
}


def _posting_module() -> type:
    router = APIRouter()

    @router.get("")
    def page() -> Response:
        return PlainTextResponse("page")

    @router.post("/act")
    def act() -> Response:
        return PlainTextResponse("acted")

    def register(r: Registry) -> None:
        r.ui_page("poster", router, path="/ui/poster", title="Poster")

    return make_module("poster", register=register)


@pytest.fixture(autouse=True)
def fast_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fast_hashes(monkeypatch)


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    rt = make_runtime(tmp_path, (HelloModule, FilesUiModule, _posting_module()))
    yield rt
    rt.close()


@pytest.fixture
def app(runtime: Runtime) -> FastAPI:
    return create_app(runtime, setup_token=SetupToken(SETUP_TOKEN))


def _routes(app: FastAPI) -> list[tuple[str, str, object]]:
    found: list[tuple[str, str, object]] = []
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if not isinstance(route, APIRoute):
            continue
        path = re.sub(r"\{[^}]+\}", "1", context.path or route.path)
        for method in sorted(route.methods or ()):
            if method != "HEAD":
                found.append((method, path, route.endpoint))
    return found


def test_the_route_list_includes_module_pages(app: FastAPI) -> None:
    paths = {path for _, path, _ in _routes(app)}
    assert {"/ui/hello", "/ui/poster", "/ui/poster/act", "/ui/settings"} <= paths
    assert {"/ui/files", "/ui/files/raw", "/ui/files/move", "/ui/files/delete"} <= paths


def test_only_the_listed_routes_are_public(app: FastAPI) -> None:
    levels = {(m, p): level_of(e) for m, p, e in _routes(app)}
    assert {k for k, v in levels.items() if v == "public"} == PUBLIC
    assert {k for k, v in levels.items() if v == "setup"} == SETUP_ONLY
    assert {k for k, v in levels.items() if v == "principal"} == TOKEN_OK


def _refused(response: httpx2.Response, *, setup_mode: bool) -> bool:
    if response.status_code == 303:
        location = response.headers["location"]
        return location == "/setup" if setup_mode else location.startswith(("/login", "/setup"))
    return response.status_code in (401, 403, 503)


@pytest.mark.parametrize("setup_mode", [True, False])
def test_every_route_refuses_anonymous_requests(
    app: FastAPI, runtime: Runtime, setup_mode: bool
) -> None:
    if not setup_mode:
        make_owner(runtime)
    with TestClient(app, follow_redirects=False) as client:
        for method, path, _ in _routes(app):
            if (method, path) in PUBLIC:
                continue
            response = client.request(method, path, data={"x": "1"})
            assert _refused(response, setup_mode=setup_mode), (method, path, response.status_code)


def test_setup_only_routes_close_once_the_owner_exists(app: FastAPI, runtime: Runtime) -> None:
    make_owner(runtime)
    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set("steward_setup", SETUP_TOKEN)
        for method, path in SETUP_ONLY:
            response = client.request(method, path, data={"x": "1"})
            assert response.headers["location"] == "/login", (method, path)


def test_every_state_changing_route_needs_the_csrf_token(app: FastAPI, runtime: Runtime) -> None:
    make_owner(runtime)
    with TestClient(app, follow_redirects=False) as client:
        log_in(client)
        checked = 0
        for method, path, _ in _routes(app):
            if method in SAFE_METHODS or (method, path) in SETUP_ONLY:
                continue  # setup-only routes are closed here (tested above)
            for data in ({"x": "1"}, {"csrf_token": "forged"}):
                response = client.request(method, path, data=data)
                assert response.status_code == 403, (method, path, response.status_code)
            checked += 1
        assert checked >= 10


def test_api_tokens_only_reach_token_routes(app: FastAPI, runtime: Runtime) -> None:
    make_owner(runtime)
    user = runtime.auth.get_user(username=USERNAME)
    _, secret = runtime.auth.issue_token(user.user_id, "t", ["read", "propose"], actor="test")
    headers = {"Authorization": f"Bearer {secret}"}
    with TestClient(app, follow_redirects=False) as client:
        for method, path, _ in _routes(app):
            if (method, path) in PUBLIC | TOKEN_OK:
                continue
            response = client.request(method, path, data={"x": "1"}, headers=headers)
            assert response.status_code in (303, 403), (method, path, response.status_code)
            if response.status_code == 303:  # setup-only routes
                assert (method, path) in SETUP_ONLY


def test_unknown_paths_are_plain_404s(app: FastAPI) -> None:
    with TestClient(app, follow_redirects=False) as client:
        assert client.get("/nope").status_code == 404
        assert client.get("/static/../pyproject.toml").status_code == 404
