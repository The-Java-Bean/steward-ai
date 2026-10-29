# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Plan §9.6: locked-folder files never come back from any route without a current
password check. Every route of the app (core, `/api/v1`, module pages) is called with
paths inside locked folders in every parameter it might read, by a session whose last
password check isn't recent and by an API token; no answer may contain a locked file's
name or content. With a recent check, only the routes meant to show locked folders may."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from api_support import log_in
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from locked_support import SECRET_CONTENT, SECRET_NAME, LockedEnv
from support import use_fast_hashes

from steward.api import create_app
from steward.core.db import utcnow
from steward.core.folders import FolderRef, load_policy
from steward.modules.shares import store as shares_store

LOCKED_PATHS = (
    "lib:vault",
    "lib:vault/inner",
    "lib:vault/zebra-secret.txt",
    "lib:private",
    "lib:private/diary.txt",
)
SKIP_POSTS = {
    "/logout",
    "/ui/settings/password",
    "/ui/settings/sessions/revoke",
    "/ui/settings/sessions/revoke-others",
}
"""Would end the session (and so make the rest of the walk anonymous)."""
SHOWS_LOCKED = (
    "/ui/locked",
    "/api/v1/search",
    "/api/v1/uploads",
    "/ui/journal",
    "/api/v1/journal",
)
"""Routes meant to show locked content after a recent password check."""


@pytest.fixture(autouse=True)
def fast_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fast_hashes(monkeypatch)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[LockedEnv]:
    e = LockedEnv(tmp_path)
    # Something in the journal about a locked folder, and a locked file in the trash.
    e.runtime.executor.run("file.mkdir", {"path": "lib:vault/made"}, actor="t", confirmed=True)
    e.runtime.executor.run(
        "file.delete", {"path": f"lib:vault/inner/{SECRET_NAME}"}, actor="t", confirmed=True
    )
    # A share made before its folder was locked (shares can't be made in locked folders).
    with e.runtime.dbs.state.write() as conn:
        shares_store.insert_share(
            conn,
            share_id="1" * 32,
            token="T" * 22,
            path=f"lib:private/{SECRET_NAME}",
            kind="file",
            password_hash=None,
            expires_at=None,
            snapshot_files=None,
            actor="t",
            now=utcnow(),
        )
    e.scan()
    yield e
    e.runtime.close()


def _routes(app: FastAPI) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if not isinstance(route, APIRoute):
            continue
        path = re.sub(r"\{[^}]+\}", "1", context.path or route.path)
        for method in sorted(route.methods or ()):
            if method != "HEAD":
                found.append((method, path))
    return found


def _variants(locked: str) -> list[dict[str, Any]]:
    """Every parameter name a route reads paths, folders or queries from."""
    base = {
        "path": [locked],
        "folder": locked,
        "to": locked,
        "q": "okapi zebra",
        "name": "upload.txt",
        "new_name": "x.txt",
        "entries": "1,2,3",
        "back": locked,
        "inline": "1",
        "library": "lib",
        "locked": "true",
        "permanent": "true",
    }
    return [
        {**base, "scope": "locked", "to_scope": "locked", "kind": "text"},
        {**base, "scope": "normal", "to_scope": "normal"},
    ]


def _json(locked: str, scope: str) -> dict[str, Any]:
    return {"path": locked, "folder": locked, "name": "x", "size": 1, "scope": scope}


def _leaks(text: str) -> bool:
    return SECRET_CONTENT in text or SECRET_NAME in text


def _walk(client: TestClient, app: FastAPI, *, gets_only: bool = False) -> list[str]:
    leaks: list[str] = []
    for method, path in _routes(app):
        if method != "GET" and (gets_only or path in SKIP_POSTS):
            continue
        for locked in LOCKED_PATHS:
            for params in _variants(locked):
                if method == "GET":
                    response = client.get(path, params=params)
                elif path.startswith("/api/"):
                    response = client.request(
                        method, path, params=params, json=_json(locked, params["scope"])
                    )
                else:
                    response = client.request(method, path, params=params, data=params)
                body = response.text if response.status_code != 303 else ""
                if path.startswith("/s/"):
                    response = client.get("/s/" + "T" * 22)
                    body += response.text
                location = response.headers.get("location", "")
                if _leaks(body) or SECRET_NAME in location:
                    leaks.append(f"{method} {path} {params['scope']} {locked}")
    return leaks


def _still_intact(env: LockedEnv) -> None:
    with env.runtime.dbs.state.read() as conn:
        policy = load_policy(conn)
    for relpath in ("vault", "vault/own", "private"):
        assert policy.settings_for(FolderRef("lib", relpath)).locked, relpath
    assert (env.root / "vault/zebra-secret.txt").exists()
    assert (env.root / "private/diary.txt").exists()


def test_no_route_shows_locked_files_without_a_recent_password_check(env: LockedEnv) -> None:
    app = create_app(env.runtime)
    with TestClient(app, follow_redirects=False) as client:
        client.headers["X-CSRF-Token"] = log_in(client)
        env.forget_reauth()
        leaks = _walk(client, app)
        assert client.get("/ui/settings").status_code == 200  # still logged in
    assert leaks == []
    _still_intact(env)


def test_no_route_shows_locked_files_to_an_api_token(env: LockedEnv) -> None:
    user = env.runtime.auth.list_users()[0]
    _, secret = env.runtime.auth.issue_token(
        user.user_id, "walk", ["read", "propose"], actor="test"
    )
    app = create_app(env.runtime)
    with TestClient(app, follow_redirects=False) as client:
        client.headers["Authorization"] = f"Bearer {secret}"
        assert client.get("/api/v1/me").status_code == 200
        leaks = _walk(client, app)
    assert leaks == []
    _still_intact(env)


def test_with_a_recent_check_only_the_locked_views_show_locked_files(env: LockedEnv) -> None:
    app = create_app(env.runtime)
    with TestClient(app, follow_redirects=False) as client:
        client.headers["X-CSRF-Token"] = log_in(client)  # a login counts as a check
        leaks = _walk(client, app, gets_only=True)
        found = client.get("/ui/locked/search", params={"q": "okapi"})
    assert _leaks(found.text)  # the walk can see a leak when there is one
    unexpected = [leak for leak in leaks if not leak.split()[1].startswith(SHOWS_LOCKED)]
    assert unexpected == []
    assert any(leak.split()[1].startswith("/ui/locked") for leak in leaks)
    _still_intact(env)
