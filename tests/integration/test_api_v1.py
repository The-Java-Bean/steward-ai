# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`/api/v1` and the core UI pages: what tokens and sessions can see and do, and that
locked folders stay out of both."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from api_support import USERNAME, csrf_of, log_in, make_owner
from fastapi.testclient import TestClient
from support import make_runtime, use_fast_hashes

from steward.api import create_app
from steward.core.config import DiscoverySettings
from steward.core.discovery import Scanner
from steward.core.folders import FolderRef, store
from steward.core.runtime import Runtime

ACTOR = "user:1"


@pytest.fixture(autouse=True)
def fast_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fast_hashes(monkeypatch)


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    rt = make_runtime(tmp_path)
    root = tmp_path / "lib"
    for relpath in ("docs/report.txt", "docs/notes.md", "private/diary.txt"):
        (root / relpath).parent.mkdir(parents=True, exist_ok=True)
        (root / relpath).write_text(relpath)
    with rt.dbs.state.write() as conn:
        store.upsert_library(conn, "lib", root=str(root), actor=ACTOR)
        store.set_type(conn, FolderRef("lib", "private"), "private", actor=ACTOR)
        store.ensure_default_roles(conn, actor=ACTOR)
    (root / ".steward/trash").mkdir(parents=True)  # steward-ai's own folder
    Scanner(rt.dbs, DiscoverySettings()).scan("lib")
    make_owner(rt)
    yield rt
    rt.close()


@pytest.fixture
def client(runtime: Runtime) -> Iterator[TestClient]:
    with TestClient(create_app(runtime), follow_redirects=False) as c:
        yield c


def _token(runtime: Runtime, scopes: list[str]) -> dict[str, str]:
    user = runtime.auth.get_user(username=USERNAME)
    _, secret = runtime.auth.issue_token(user.user_id, "t", scopes, actor="test")
    return {"Authorization": f"Bearer {secret}"}


def test_tokens_read_but_never_see_locked_folders(client: TestClient, runtime: Runtime) -> None:
    headers = _token(runtime, ["read"])
    libs = client.get("/api/v1/libraries", headers=headers).json()
    assert [lib["library_id"] for lib in libs["libraries"]] == ["lib"]
    folder = client.get("/api/v1/folders", params={"path": "lib:"}, headers=headers).json()
    assert "docs" in folder["subfolders"]
    assert "private" not in folder["subfolders"]
    assert ".steward" not in folder["subfolders"]
    listed = client.get("/api/v1/files", params={"path": "lib:docs"}, headers=headers).json()
    assert sorted(f["path"] for f in listed["files"]) == [
        "lib:docs/notes.md",
        "lib:docs/report.txt",
    ]
    file_id = listed["files"][0]["file_id"]
    assert client.get(f"/api/v1/files/{file_id}", headers=headers).status_code == 200
    for path in ("lib:private", "lib:private/sub"):
        assert (
            client.get("/api/v1/folders", params={"path": path}, headers=headers).status_code == 404
        )
        assert (
            client.get("/api/v1/files", params={"path": path}, headers=headers).status_code == 404
        )
    hits = client.get("/api/v1/search", params={"q": "txt"}, headers=headers).json()["hits"]
    assert [h["name"] for h in hits] == ["report.txt"]
    locked = client.get("/api/v1/search", params={"q": "diary", "locked": "true"}, headers=headers)
    assert locked.status_code == 403


def test_tokens_without_read_scope_are_refused(client: TestClient, runtime: Runtime) -> None:
    headers = _token(runtime, ["propose"])
    assert client.get("/api/v1/libraries", headers=headers).status_code == 403


def test_tokens_cant_change_anything(client: TestClient, runtime: Runtime, tmp_path: Path) -> None:
    headers = _token(runtime, ["read", "propose"])
    for path in ("/api/v1/actions/file.mkdir", "/api/v1/journal/1/undo", "/api/v1/jobs/1/retry"):
        response = client.post(path, json={"path": "lib:new"}, headers=headers)
        assert response.status_code == 403, path
    assert not (tmp_path / "lib/new").exists()


def test_session_runs_and_undoes_actions(
    client: TestClient, runtime: Runtime, tmp_path: Path
) -> None:
    csrf = log_in(client)
    h = {"X-CSRF-Token": csrf}
    preview = client.post("/api/v1/actions/file.mkdir/preview", json={"path": "lib:new"}, headers=h)
    assert preview.status_code == 200, preview.text
    assert not (tmp_path / "lib/new").exists()
    made = client.post("/api/v1/actions/file.mkdir", json={"path": "lib:new"}, headers=h)
    assert made.status_code == 201, made.text
    assert (tmp_path / "lib/new").is_dir()
    entry_id = made.json()["entry_id"]
    journal = client.get("/api/v1/journal").json()["entries"]
    assert journal[0]["entry_id"] == entry_id
    assert journal[0]["undoable"]
    undone = client.post(f"/api/v1/journal/{entry_id}/undo", headers=h)
    assert undone.status_code == 201, undone.text
    assert not (tmp_path / "lib/new").exists()
    again = client.post(f"/api/v1/journal/{entry_id}/undo", headers=h)
    assert again.status_code == 409


def test_journal_hides_locked_folder_actions_until_reauth(
    client: TestClient, runtime: Runtime
) -> None:
    runtime.executor.run(
        "file.mkdir", {"path": "lib:private/secret plans"}, actor=ACTOR, confirmed=True
    )
    headers = _token(runtime, ["read"])
    entries = client.get("/api/v1/journal", headers=headers).json()["entries"]
    assert entries[0]["locked"]
    assert "secret plans" not in entries[0]["summary"]
    log_in(client)  # logging in counts as a recent password check
    entries = client.get("/api/v1/journal").json()["entries"]
    assert "secret plans" in entries[0]["summary"]


def test_openapi_lists_only_the_api(client: TestClient, runtime: Runtime) -> None:
    spec = client.get("/api/v1/openapi.json", headers=_token(runtime, ["read"])).json()
    assert spec["info"]["title"] == "steward-ai API"
    assert "/api/v1/search" in spec["paths"]
    assert all(p.startswith("/api/") for p in spec["paths"])
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404


def test_jobs_and_health(client: TestClient, runtime: Runtime) -> None:
    headers = _token(runtime, ["read"])
    jobs = client.get("/api/v1/jobs", headers=headers).json()
    assert all(j["last_error"] is None for j in jobs["jobs"])
    health = client.get("/api/v1/health", headers=headers).json()
    names = {c["name"] for c in health["checks"]}
    assert {"core.backup", "core.jobs", "core.models"} <= names


def test_ui_pages_render(client: TestClient) -> None:
    log_in(client)
    for path in (
        "/ui/search?q=report",
        "/ui/journal",
        "/ui/jobs",
        "/ui/health",
        "/ui/settings/folders",
    ):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text[:500])
    page = client.get("/ui/search?q=txt").text
    assert "report.txt" in page
    assert "diary" not in page


def test_ui_folder_unlock_needs_reauth(client: TestClient, runtime: Runtime) -> None:
    runtime.auth.set_settings_from_host({"reauth_minutes": 0}, actor="test")
    csrf = log_in(client)  # the login is one password check, used up by one step

    def unlock() -> str:
        response = client.post(
            "/ui/settings/folders/lock",
            data={"csrf_token": csrf, "folder": "lib:private", "locked": "false"},
        )
        assert response.status_code == 303, response.text
        return str(response.headers["location"])

    assert unlock().endswith("folder_unlocked")
    runtime.executor.run("folder.lock", {"path": "lib:private"}, actor=ACTOR)
    assert unlock().startswith("/reauth")


def test_ui_lock_and_map(client: TestClient, runtime: Runtime) -> None:
    csrf = log_in(client)
    response = client.post(
        "/ui/settings/folders/lock", data={"csrf_token": csrf, "folder": "lib:docs"}
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"].endswith("folder_locked")
    hits = client.get("/api/v1/search", params={"q": "report"}).json()["hits"]
    assert hits == []
    response = client.post(
        "/ui/settings/folders/map",
        data={"csrf_token": csrf_of(client), "folder": "lib:docs", "type_name": "nope"},
    )
    assert response.status_code == 400
