# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from api_support import (
    PASSWORD,
    SETUP_TOKEN,
    USERNAME,
    csrf_of,
    finish_setup,
    form_token,
    log_in,
    make_owner,
)
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from sqlalchemy import update
from starlette.responses import Response
from steward_hello import HelloModule
from support import make_module, make_runtime, use_fast_hashes

from steward.api import UiPageError, create_app
from steward.api.security import SESSION_COOKIE, safe_next
from steward.core import setup
from steward.core.db.schema import sessions
from steward.core.folders import FolderRef, load_policy
from steward.core.runtime import Runtime
from steward.core.setup import SetupToken
from steward.sdk import Registry


@pytest.fixture(autouse=True)
def fast_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fast_hashes(monkeypatch)


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "libraries" / "main"
    for name in ("Documents", "Photos", "Personal"):
        (root / name).mkdir(parents=True)
    (root / "Documents" / "a.txt").write_text("text")
    return root


def _runtime(tmp_path: Path, modules: tuple[type, ...] = ()) -> Runtime:
    return make_runtime(
        tmp_path,
        modules,
        steward={"setup": {"library_dirs": [str(tmp_path / "libraries")]}},
    )


@pytest.fixture
def runtime(tmp_path: Path, library: Path) -> Iterator[Runtime]:
    rt = _runtime(tmp_path)
    yield rt
    rt.close()


def _client(runtime: Runtime, **kwargs: object) -> TestClient:
    app = create_app(runtime, setup_token=SetupToken(SETUP_TOKEN))
    return TestClient(app, follow_redirects=False, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def client(runtime: Runtime) -> Iterator[TestClient]:
    with _client(runtime) as c:
        yield c


@pytest.fixture
def owner_client(runtime: Runtime, client: TestClient) -> TestClient:
    make_owner(runtime)
    log_in(client)
    return client


# --- setup mode ------------------------------------------------------------------


def test_everything_but_setup_is_closed_without_an_owner(client: TestClient) -> None:
    for path in ("/", "/ui/", "/ui/settings", "/login"):
        response = client.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/setup"
    assert client.get("/api/v1/me").status_code == 503
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/setup").status_code == 200


def test_owner_step_needs_the_setup_token(client: TestClient) -> None:
    assert client.get("/setup/owner").headers["location"] == "/setup"
    page = client.get("/setup")
    wrong = client.post("/setup", data={"token": "nope", "csrf_token": form_token(page.text)})
    assert wrong.status_code == 403
    assert client.get("/setup/owner").headers["location"] == "/setup"


def test_setup_forms_need_the_presession_cookie(client: TestClient) -> None:
    client.get("/setup")
    assert client.post("/setup", data={"token": SETUP_TOKEN}).status_code == 403
    assert (
        client.post("/setup", data={"token": SETUP_TOKEN, "csrf_token": "forged"}).status_code
        == 403
    )


def test_owner_form_needs_the_presession_cookie(client: TestClient) -> None:
    _start_setup(client)
    data = {"username": USERNAME, "password": PASSWORD, "confirm": PASSWORD}
    assert client.post("/setup/owner", data=data).status_code == 403
    assert client.post("/setup/owner", data={**data, "csrf_token": "x"}).status_code == 403


def _start_setup(client: TestClient) -> str:
    page = client.get("/setup")
    token = form_token(page.text)
    response = client.post("/setup", data={"token": SETUP_TOKEN, "csrf_token": token})
    assert response.headers["location"] == "/setup/owner"
    return token


def test_owner_form_rejects_mismatched_and_weak_passwords(client: TestClient) -> None:
    token = _start_setup(client)
    mismatch = client.post(
        "/setup/owner",
        data={"username": USERNAME, "password": PASSWORD, "confirm": "x", "csrf_token": token},
    )
    assert mismatch.status_code == 400
    assert "match" in mismatch.text
    weak = client.post(
        "/setup/owner",
        data={"username": USERNAME, "password": "short", "confirm": "short", "csrf_token": token},
    )
    assert weak.status_code == 400
    assert "12 characters" in weak.text


def test_the_wizard_from_token_to_done(client: TestClient, runtime: Runtime, library: Path) -> None:
    token = _start_setup(client)
    response = client.post(
        "/setup/owner",
        data={"username": USERNAME, "password": PASSWORD, "confirm": PASSWORD, "csrf_token": token},
    )
    assert response.headers["location"] == "/setup/libraries"
    assert SESSION_COOKIE in response.cookies
    assert runtime.auth.has_owner()

    # The token steps are over once the owner exists.
    assert client.get("/setup/owner").headers["location"] == "/login"
    assert client.get("/setup").headers["location"] == "/setup/libraries"
    assert client.get("/ui/").headers["location"] == "/setup/libraries"

    page = client.get("/setup/libraries")
    assert str(library) in page.text  # found inside setup.library_dirs
    csrf = form_token(page.text)
    empty = client.post("/setup/libraries", data={"action": "next", "csrf_token": csrf})
    assert empty.status_code == 400
    added = client.post(
        "/setup/libraries",
        data={"action": "add", "library_id": "main", "path": str(library), "csrf_token": csrf},
    )
    assert added.status_code == 200, added.text
    response = client.post("/setup/libraries", data={"action": "next", "csrf_token": csrf})
    assert response.headers["location"] == "/setup/types"

    page = client.get("/setup/types")
    assert 'name="type:main:Photos"' in page.text
    response = client.post(
        "/setup/types",
        data={
            "type:main:Documents": "documents",
            "type:main:Photos": "media",
            "type:main:Personal": "",
            "csrf_token": csrf,
        },
    )
    assert response.headers["location"] == "/setup/private"

    response = client.post(
        "/setup/private",
        data={
            "action": "create",
            "library_id": "main",
            "new_name": "Vault",
            "permanent": "true",
            "csrf_token": csrf,
        },
    )
    assert response.headers["location"] == "/setup/roles"
    assert (library / "Vault").is_dir()

    response = client.post("/setup/roles", data={"csrf_token": csrf})
    assert response.headers["location"] == "/setup/done"
    assert client.get("/setup/done").status_code == 200

    with runtime.dbs.state.read() as conn:
        policy = load_policy(conn)
    assert policy.mappings[FolderRef("main", "Documents")].type_name == "documents"
    assert FolderRef("main", "Personal") not in policy.mappings
    vault = policy.settings_for(FolderRef("main", "Vault"))
    assert (vault.type.name, vault.locked, vault.permanent) == ("private", True, True)
    assert set(policy.roles) == {"trash", "edit_backups"}

    # Finished: the steps close and home opens.
    assert client.get("/setup/types").headers["location"] == "/ui/"
    assert client.get("/ui/").status_code == 200


def test_skipping_the_private_folder_warns_on_home(
    owner_client: TestClient, runtime: Runtime
) -> None:
    with runtime.dbs.state.write() as conn:
        setup.skip_private(conn, actor="test")
    assert "haven't chosen a private folder" in owner_client.get("/ui/").text


# --- login, logout, lockouts ---------------------------------------------------------


def test_login_and_logout(runtime: Runtime, client: TestClient) -> None:
    make_owner(runtime)
    assert client.get("/ui/settings").headers["location"] == "/login?next=/ui/settings"
    page = client.get("/login?next=/ui/settings")
    response = client.post(
        "/login",
        data={
            "username": "Owner",  # usernames are case-insensitive
            "password": PASSWORD,
            "next": "/ui/settings",
            "csrf_token": form_token(page.text),
        },
    )
    assert response.headers["location"] == "/ui/settings"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie or "samesite=strict" in cookie.lower()
    assert "Secure" not in cookie  # plain HTTP
    secret = client.cookies[SESSION_COOKIE]
    csrf = csrf_of(client)

    response = client.post("/logout", data={"csrf_token": csrf})
    assert response.headers["location"] == "/login"
    client.cookies.set(SESSION_COOKIE, secret)  # the old cookie is dead server-side
    assert client.get("/ui/settings").status_code == 303


def test_wrong_password_then_lockout(runtime: Runtime, client: TestClient) -> None:
    make_owner(runtime)
    token = form_token(client.get("/login").text)
    data = {"username": USERNAME, "password": "wrong password!", "csrf_token": token}
    for _ in range(10):
        assert client.post("/login", data=data).status_code == 401
    locked = client.post("/login", data={**data, "password": PASSWORD})
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0
    assert "too many failed attempts" in locked.text


def test_open_redirects_are_refused(runtime: Runtime, client: TestClient) -> None:
    make_owner(runtime)
    token = form_token(client.get("/login").text)
    response = client.post(
        "/login",
        data={
            "username": USERNAME,
            "password": PASSWORD,
            "next": "//evil.example/",
            "csrf_token": token,
        },
    )
    assert response.headers["location"] == "/ui/"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/ui/settings", "/ui/settings"),
        ("/ui/x?a=1", "/ui/x?a=1"),
        ("//evil.example", "/ui/"),
        ("/\\evil.example", "/ui/"),
        ("https://evil.example/", "/ui/"),
        ("javascript:alert(1)", "/ui/"),
        ("/ok\r\nSet-Cookie: x", "/ui/"),
        (None, "/ui/"),
    ],
)
def test_safe_next(value: str | None, expected: str) -> None:
    assert safe_next(value) == expected


def test_secure_cookie_over_https(runtime: Runtime) -> None:
    make_owner(runtime)
    with _client(runtime, base_url="https://testserver") as client:
        token = form_token(client.get("/login").text)
        response = client.post(
            "/login", data={"username": USERNAME, "password": PASSWORD, "csrf_token": token}
        )
    assert "Secure" in response.headers["set-cookie"]


def test_security_headers(client: TestClient) -> None:
    headers = client.get("/setup").headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["cache-control"] == "no-store"


# --- CSRF and re-auth ---------------------------------------------------------------


def test_posts_need_the_session_csrf_token(owner_client: TestClient) -> None:
    url = "/ui/settings/sessions/revoke-others"
    assert owner_client.post(url).status_code == 403
    assert owner_client.post(url, data={"csrf_token": "forged"}).status_code == 403
    csrf = csrf_of(owner_client)
    assert owner_client.post(url, headers={"X-CSRF-Token": csrf}).status_code == 303
    assert owner_client.post(url, data={"csrf_token": csrf}).status_code == 303


def test_cross_site_posts_are_refused(owner_client: TestClient) -> None:
    csrf = csrf_of(owner_client)
    response = owner_client.post(
        "/ui/settings/sessions/revoke-others",
        data={"csrf_token": csrf},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403


def _forget_reauth(runtime: Runtime) -> None:
    with runtime.dbs.state.write() as conn:
        conn.execute(update(sessions).values(reauth_at=None))


def test_sensitive_steps_redirect_to_reauth_and_back(
    owner_client: TestClient, runtime: Runtime
) -> None:
    csrf = csrf_of(owner_client)
    _forget_reauth(runtime)
    response = owner_client.post(
        "/ui/settings/tokens",
        data={"label": "script", "scopes": ["read"], "csrf_token": csrf},
        headers={"Referer": "http://testserver/ui/settings"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/reauth?next=/ui/settings"

    wrong = owner_client.post(
        "/reauth", data={"password": "wrong password!", "next": "/ui/settings", "csrf_token": csrf}
    )
    assert wrong.status_code == 401
    ok = owner_client.post(
        "/reauth", data={"password": PASSWORD, "next": "/ui/settings", "csrf_token": csrf}
    )
    assert ok.headers["location"] == "/ui/settings"

    created = owner_client.post(
        "/ui/settings/tokens",
        data={"label": "script", "scopes": ["read"], "expires_days": "30", "csrf_token": csrf},
    )
    assert created.status_code == 200
    assert "stw_" in created.text


def test_login_settings_need_reauth_and_warn_when_off(
    owner_client: TestClient, runtime: Runtime
) -> None:
    csrf = csrf_of(owner_client)
    _forget_reauth(runtime)
    data = {"reauth_minutes": "5", "csrf_token": csrf}  # checkbox unticked: re-auth off
    assert owner_client.post("/ui/settings/login", data=data).status_code == 303
    assert runtime.auth.settings().reauth_enabled is True  # refused: went to /reauth
    owner_client.post("/reauth", data={"password": PASSWORD, "csrf_token": csrf})
    assert (
        owner_client.post("/ui/settings/login", data=data)
        .headers["location"]
        .endswith("settings_saved")
    )
    assert runtime.auth.settings().reauth_enabled is False
    assert "Re-auth is off" in owner_client.get("/ui/").text


def test_change_password(owner_client: TestClient, runtime: Runtime) -> None:
    csrf = csrf_of(owner_client)
    new = "a different long passphrase"
    wrong = owner_client.post(
        "/ui/settings/password",
        data={"current": "nope nope nope", "new": new, "confirm": new, "csrf_token": csrf},
    )
    assert wrong.status_code == 400
    ok = owner_client.post(
        "/ui/settings/password",
        data={"current": PASSWORD, "new": new, "confirm": new, "csrf_token": csrf},
    )
    assert ok.headers["location"] == "/ui/settings?notice=password_changed"
    assert (
        "Your password was changed" in owner_client.get("/ui/settings?notice=password_changed").text
    )
    # This session survives; a new login needs the new password.
    assert owner_client.get("/ui/settings").status_code == 200


def test_revoke_sessions_and_tokens(owner_client: TestClient, runtime: Runtime) -> None:
    user = runtime.auth.get_user(username=USERNAME)
    _, secret = runtime.auth.issue_token(user.user_id, "t", ["read"], actor="test")
    token_id = runtime.auth.list_tokens()[0].token_id
    other = runtime.auth.login(USERNAME, PASSWORD, ip="198.51.100.9")
    csrf = csrf_of(owner_client)

    assert (
        owner_client.post(
            "/ui/settings/sessions/revoke",
            data={"session_id": str(other.principal.session_id), "csrf_token": csrf},
        )
        .headers["location"]
        .endswith("session_revoked")
    )
    assert runtime.auth.authenticate_session(other.secret) is None
    assert (
        owner_client.post(
            "/ui/settings/sessions/revoke", data={"session_id": "999", "csrf_token": csrf}
        ).status_code
        == 404
    )

    assert (
        owner_client.post(
            "/ui/settings/tokens/revoke", data={"token_id": str(token_id), "csrf_token": csrf}
        ).status_code
        == 303
    )
    assert runtime.auth.authenticate_token(secret) is None


# --- API tokens ---------------------------------------------------------------------


def test_bearer_tokens(runtime: Runtime, client: TestClient) -> None:
    make_owner(runtime)
    user = runtime.auth.get_user(username=USERNAME)
    _, secret = runtime.auth.issue_token(user.user_id, "script", ["read"], actor="test")
    auth = {"Authorization": f"Bearer {secret}"}
    me = client.get("/api/v1/me", headers=auth)
    assert me.json() == {"username": USERNAME, "role": "owner", "via": "token", "scopes": ["read"]}
    # Tokens can't use browser pages, and need no CSRF token but can't do session things.
    assert client.get("/ui/settings", headers=auth).status_code == 403
    assert client.post("/ui/settings/tokens", data={"label": "x"}, headers=auth).status_code == 403
    assert client.get("/api/v1/me").status_code == 401
    assert client.get("/api/v1/me", headers={"Authorization": "Bearer stw_x"}).status_code == 401
    assert client.get("/api/v1/me", headers={"Authorization": "Basic abc"}).status_code == 401


def test_a_bad_bearer_token_never_falls_back_to_the_cookie(owner_client: TestClient) -> None:
    assert owner_client.get("/api/v1/me").json()["via"] == "session"
    response = owner_client.get("/api/v1/me", headers={"Authorization": "Bearer stw_nope"})
    assert response.status_code == 401


# --- module pages -------------------------------------------------------------------


def test_hello_page_is_behind_login_and_goes_away_when_disabled(
    tmp_path: Path, library: Path
) -> None:
    runtime = _runtime(tmp_path, (HelloModule,))
    try:
        make_owner(runtime)
        with _client(runtime) as client:
            assert client.get("/ui/hello").headers["location"] == "/login?next=/ui/hello"
            log_in(client)
            assert client.get("/ui/hello").text.endswith("<h1>hello</h1>")
            assert 'href="/ui/hello"' in client.get("/ui/").text
    finally:
        runtime.close()

    runtime = _runtime(tmp_path)
    try:
        with _client(runtime) as client:
            log_in(client)
            assert client.get("/ui/hello").status_code == 404
            assert 'href="/ui/hello"' not in client.get("/ui/").text
    finally:
        runtime.close()


def _page_module(name: str, router: APIRouter, path: str) -> type:
    def register(r: Registry) -> None:
        r.ui_page(name, router, path=path, title=name.title())

    return make_module(name, register=register)


def test_module_pages_cant_opt_out_of_login_or_csrf(tmp_path: Path, library: Path) -> None:
    router = APIRouter()

    def sneaky() -> Response:
        return PlainTextResponse("secret")

    sneaky._steward_access = "public"  # type: ignore[attr-defined]  # has no effect
    router.add_api_route("", sneaky, methods=["GET"])

    @router.post("/do")
    def do() -> Response:
        return PlainTextResponse("done")

    runtime = _runtime(tmp_path, (_page_module("mod", router, "/ui/mod"),))
    try:
        make_owner(runtime)
        with _client(runtime) as client:
            assert client.get("/ui/mod").status_code == 303
            csrf = log_in(client)
            assert client.get("/ui/mod").text == "secret"
            assert client.post("/ui/mod/do").status_code == 403
            assert client.post("/ui/mod/do", data={"csrf_token": csrf}).text == "done"
    finally:
        runtime.close()


def test_module_pages_cant_take_core_paths_or_bypass_routes(tmp_path: Path, library: Path) -> None:
    reserved = _page_module("mod", APIRouter(), "/ui/settings")
    runtime = _runtime(tmp_path, (reserved,))
    try:
        with pytest.raises(UiPageError, match="reserved"):
            create_app(runtime)
    finally:
        runtime.close()

    raw = APIRouter()
    raw.add_route("/raw", lambda request: PlainTextResponse("x"))
    runtime = _runtime(tmp_path, (_page_module("raw", raw, "/ui/raw"),))
    try:
        with pytest.raises(UiPageError, match="only regular HTTP routes"):
            create_app(runtime)
    finally:
        runtime.close()


def test_two_modules_cant_share_a_page_path(tmp_path: Path, library: Path) -> None:
    a = _page_module("a", APIRouter(), "/ui/same")
    b = _page_module("b", APIRouter(), "/ui/same")
    runtime = _runtime(tmp_path, (a, b))
    try:
        with pytest.raises(UiPageError, match="already used"):
            create_app(runtime)
    finally:
        runtime.close()


def test_unfinished_setup_resumes_after_login(runtime: Runtime, client: TestClient) -> None:
    make_owner(runtime, finished=False)
    log_in(client)
    assert client.get("/ui/").headers["location"] == "/setup/libraries"
    finish_setup(runtime)
    assert client.get("/ui/").status_code == 200
