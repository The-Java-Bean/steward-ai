# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The Locked Folder (`/ui/locked`, plan §9.7): behind a password check, the same file
manager in the locked scope; lock, permanent locks, unlock with a preview, moves out
with a confirmation, search inside."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from api_support import PASSWORD, form_token, log_in
from fastapi.testclient import TestClient
from locked_support import SECRET_CONTENT, SECRET_NAME, LockedEnv
from support import use_fast_hashes

from steward.api import create_app
from steward.core.executor import SensitiveActionError
from steward.core.folders import FolderRef, PermanentLockError, load_policy, store
from steward.sdk import ActionRefused


@pytest.fixture(autouse=True)
def fast_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fast_hashes(monkeypatch)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[LockedEnv]:
    e = LockedEnv(tmp_path)
    yield e
    e.runtime.close()


@pytest.fixture
def client(env: LockedEnv) -> Iterator[TestClient]:
    with TestClient(create_app(env.runtime), follow_redirects=False) as c:
        c.headers["X-CSRF-Token"] = log_in(c)
        yield c


def _reauth(client: TestClient) -> None:
    page = client.get("/reauth")
    done = client.post("/reauth", data={"password": PASSWORD, "csrf_token": form_token(page.text)})
    assert done.status_code == 303


def _query(location: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(location).query)


def _policy(env: LockedEnv) -> object:
    with env.runtime.dbs.state.read() as conn:
        return load_policy(conn)


def _locked(env: LockedEnv, relpath: str) -> bool:
    with env.runtime.dbs.state.read() as conn:
        return load_policy(conn).settings_for(FolderRef("lib", relpath)).locked


# --- the gate ------------------------------------------------------------------------------


def test_the_locked_folder_is_in_the_nav(client: TestClient) -> None:
    assert 'href="/ui/locked"' in client.get("/ui/").text


def test_every_locked_route_asks_for_the_password_first(client: TestClient, env: LockedEnv) -> None:
    env.forget_reauth()
    for method, url, params in (
        ("GET", "/ui/locked", {}),
        ("GET", "/ui/locked", {"path": "lib:vault"}),
        ("GET", "/ui/locked/view", {"path": "lib:vault/zebra-secret.txt"}),
        ("GET", "/ui/locked/raw", {"path": "lib:vault/zebra-secret.txt"}),
        ("GET", "/ui/locked/zip", {"path": "lib:vault"}),
        ("GET", "/ui/locked/search", {"q": "zebra"}),
        ("GET", "/ui/locked/unlock", {"path": "lib:vault"}),
        ("GET", "/ui/locked/lock", {"path": "lib:vault"}),
        ("GET", "/ui/locked/move", {"path": "lib:vault/zebra-secret.txt"}),
        ("GET", "/ui/locked/delete", {"path": "lib:vault/zebra-secret.txt"}),
        ("GET", "/ui/locked/rename", {"path": "lib:vault/zebra-secret.txt"}),
    ):
        response = client.request(method, url, params=params)
        assert response.status_code == 303, (url, response.status_code)
        assert response.headers["location"].startswith("/reauth?next="), url
        assert SECRET_CONTENT not in response.text
        assert SECRET_NAME not in response.text
    posted = client.post("/ui/locked/unlock", data={"path": "lib:vault"})
    assert posted.status_code == 303
    assert posted.headers["location"].startswith("/reauth")
    assert _locked(env, "vault")
    _reauth(client)
    assert client.get("/ui/locked").status_code == 200


def test_with_a_window_of_zero_browsing_still_works_right_after_the_password(
    client: TestClient, env: LockedEnv
) -> None:
    env.runtime.auth.set_settings_from_host({"reauth_minutes": 0}, actor="test")
    env.forget_reauth()
    assert client.get("/ui/locked").headers["location"].startswith("/reauth")
    _reauth(client)
    for _ in range(3):  # viewing doesn't use up the one-shot check
        assert client.get("/ui/locked", params={"path": "lib:vault"}).status_code == 200


# --- browsing --------------------------------------------------------------------------------


def test_the_root_lists_the_outermost_locked_folders_but_not_the_trash(
    client: TestClient, env: LockedEnv
) -> None:
    client.post("/ui/locked/delete", data={"path": ["lib:vault/zebra-secret.txt"]})
    assert list((env.root / ".steward/trash").rglob("zebra-secret.txt"))
    page = client.get("/ui/locked")
    assert page.status_code == 200
    assert 'href="/ui/locked?path=lib%3Aprivate"' in page.text
    assert 'href="/ui/locked?path=lib%3Avault"' in page.text
    assert "lib:vault/own" not in page.text  # inside vault
    assert "trash" not in page.text
    assert "type private" in page.text


def test_inside_a_locked_folder_is_the_full_file_manager(
    client: TestClient, env: LockedEnv
) -> None:
    page = client.get("/ui/locked", params={"path": "lib:vault"})
    assert page.status_code == 200
    assert "zebra-secret.txt" in page.text
    assert 'href="/ui/locked?path=lib%3Avault/inner"' in page.text
    assert 'data-scope="locked"' in page.text  # uploads go to the locked scope
    assert 'action="/ui/locked/mkdir"' in page.text
    assert "/ui/files" not in page.text.split("<main", 1)[-1].split("files-tabs", 1)[-1]
    assert "Unlock…" in page.text
    inner = client.get("/ui/locked", params={"path": "lib:vault/inner"})
    assert '<a href="/ui/locked">Locked Folder</a>' in inner.text
    assert "because <a" in inner.text  # locked by its parent: no unlock button
    assert "Unlock…" not in inner.text

    view = client.get("/ui/locked/view", params={"path": "lib:vault/zebra-secret.txt"})
    assert SECRET_CONTENT in view.text
    raw = client.get("/ui/locked/raw", params={"path": "lib:vault/zebra-secret.txt"})
    assert raw.text == f"{SECRET_CONTENT} vault\n"
    zipped = client.get("/ui/locked/zip", params={"path": "lib:vault"})
    assert zipped.status_code == 200
    assert SECRET_NAME.encode() in zipped.content


def test_normal_paths_are_not_found_in_the_locked_scope(client: TestClient) -> None:
    for url in ("/ui/locked", "/ui/locked/view", "/ui/locked/raw"):
        response = client.get(url, params={"path": "lib:docs/report.txt"})
        assert response.status_code == 404, url
    assert client.get("/ui/locked", params={"path": "lib:"}).status_code == 404


def test_changes_inside_a_locked_folder(client: TestClient, env: LockedEnv) -> None:
    made = client.post("/ui/locked/mkdir", data={"folder": "lib:vault", "name": "new"})
    assert made.status_code == 303
    assert made.headers["location"].startswith("/ui/locked?path=lib:vault&did=mkdir")
    assert (env.root / "vault/new").is_dir()
    renamed = client.post(
        "/ui/locked/rename", data={"path": "lib:vault/zebra-secret.txt", "new_name": "z.txt"}
    )
    assert renamed.status_code == 303
    assert (env.root / "vault/z.txt").exists()
    deleted = client.post("/ui/locked/delete", data={"path": ["lib:vault/z.txt"]})
    assert deleted.status_code == 303
    trashed = list((env.root / ".steward/trash").rglob("z.txt"))
    assert len(trashed) == 1
    assert "/locked/" in str(trashed[0])
    # The trashed file is in neither view.
    assert client.get("/ui/locked/search", params={"q": "z.txt"}).text.count("/view?") == 0
    undo = _query(deleted.headers["location"])["undo"][0]
    undone = client.post("/ui/locked/undo", data={"entries": undo, "back": "lib:vault"})
    assert undone.status_code == 303
    assert undone.headers["location"].startswith("/ui/locked?path=lib:vault")
    assert (env.root / "vault/z.txt").exists()


def test_moves_inside_the_locked_folder_need_no_confirmation(
    client: TestClient, env: LockedEnv
) -> None:
    picker = client.get("/ui/locked/move", params={"path": "lib:vault/zebra-secret.txt"})
    assert picker.status_code == 200
    assert "Out of the Locked Folder" in picker.text
    moved = client.post(
        "/ui/locked/move",
        data={"path": ["lib:vault/zebra-secret.txt"], "to": "lib:private", "to_scope": "locked"},
    )
    assert moved.status_code == 303, moved.text
    assert (env.root / "private/zebra-secret.txt").exists()


def test_moving_out_needs_a_confirmation(client: TestClient, env: LockedEnv) -> None:
    picker = client.get(
        "/ui/locked/move", params={"path": "lib:vault/zebra-secret.txt", "to_scope": "normal"}
    )
    assert picker.status_code == 200
    assert "confirm_out" in picker.text
    assert "Move here (lib)" in picker.text
    data = {"path": ["lib:vault/zebra-secret.txt"], "to": "lib:docs", "to_scope": "normal"}
    refused = client.post("/ui/locked/move", data=data)
    assert refused.status_code == 409
    assert (env.root / "vault/zebra-secret.txt").exists()
    moved = client.post("/ui/locked/move", data={**data, "confirm_out": "true"})
    assert moved.status_code == 303, moved.text
    assert moved.headers["location"].startswith("/ui/files?path=lib:docs&did=move_out")
    assert (env.root / "docs/zebra-secret.txt").exists()
    assert "zebra-secret.txt" in client.get("/ui/files", params={"path": "lib:docs"}).text


def test_moving_out_asks_for_the_password_when_it_is_not_recent(
    client: TestClient, env: LockedEnv
) -> None:
    env.runtime.auth.set_settings_from_host({"reauth_minutes": 0}, actor="test")
    env.forget_reauth()
    _reauth(client)
    client.get("/ui/locked", params={"path": "lib:vault"})  # browsing: not used up
    data = {
        "path": ["lib:vault/zebra-secret.txt"],
        "to": "lib:docs",
        "to_scope": "normal",
        "confirm_out": "true",
    }
    first = client.post("/ui/locked/move", data=data)
    assert first.status_code == 303
    assert first.headers["location"].startswith("/ui/files")
    # The one-shot check is used up; the next move out asks again.
    data["path"] = ["lib:vault/inner"]
    second = client.post("/ui/locked/move", data=data)
    assert second.status_code == 303
    assert second.headers["location"].startswith("/reauth")
    assert (env.root / "vault/inner").is_dir()


def test_normal_views_never_offer_moving_into_the_locked_scope(client: TestClient) -> None:
    response = client.post(
        "/ui/files/move",
        data={"path": ["lib:docs/report.txt"], "to": "lib:vault", "to_scope": "locked"},
    )
    assert response.status_code == 404


# --- search and kinds --------------------------------------------------------------------


def test_search_inside_the_locked_folder(client: TestClient) -> None:
    found = client.get("/ui/locked/search", params={"q": "okapi"})
    assert found.status_code == 200
    assert found.text.count(SECRET_NAME) >= 2  # private/ and vault/inner/
    assert 'href="/ui/locked/view?path=lib%3Avault/inner/okapi-hidden.txt"' in found.text
    assert "report.txt" not in client.get("/ui/locked/search", params={"q": "report"}).text
    # The normal search never finds locked files.
    assert SECRET_NAME not in client.get("/ui/search", params={"q": "okapi"}).text
    too_many = client.get("/ui/locked/search", params={"q": " ".join("w" * 11)})
    assert too_many.status_code == 200


def test_files_by_kind_in_the_locked_scope(client: TestClient) -> None:
    locked = client.get("/ui/locked", params={"kind": "text"})
    assert SECRET_NAME in locked.text
    assert "report.txt" not in locked.text
    normal = client.get("/ui/files", params={"kind": "text"})
    assert SECRET_NAME not in normal.text
    assert "report.txt" in normal.text


# --- lock, permanent, unlock ---------------------------------------------------------------


def test_lock_a_folder_from_the_file_manager_and_undo_it(
    client: TestClient, env: LockedEnv
) -> None:
    docs = client.get("/ui/files", params={"path": "lib:docs"})
    assert 'href="/ui/files/lock?path=lib%3Adocs"' in docs.text
    form = client.get("/ui/files/lock", params={"path": "lib:docs"})
    assert form.status_code == 200
    assert 'name="permanent"' in form.text
    locked = client.post("/ui/files/lock", data={"path": "lib:docs"})
    assert locked.status_code == 303
    assert locked.headers["location"].startswith("/ui/files?path=lib:&did=lock")
    assert _locked(env, "docs")
    assert client.get("/ui/files", params={"path": "lib:docs"}).status_code == 404
    assert "report.txt" not in client.get("/ui/search", params={"q": "report"}).text
    assert 'href="/ui/locked?path=lib%3Adocs"' in client.get("/ui/locked").text
    undo = _query(locked.headers["location"])["undo"][0]
    undone = client.post("/ui/files/undo", data={"entries": undo, "back": "lib:"})
    assert undone.status_code == 303
    assert not _locked(env, "docs")


def test_a_library_root_has_no_lock_button(client: TestClient) -> None:
    root = client.get("/ui/files", params={"path": "lib:"})
    assert "/ui/files/lock" not in root.text


def test_a_permanent_lock_asks_for_the_password_and_cant_be_undone(
    client: TestClient, env: LockedEnv
) -> None:
    env.runtime.auth.set_settings_from_host({"reauth_minutes": 0}, actor="test")
    env.forget_reauth()
    asked = client.post("/ui/files/lock", data={"path": "lib:docs", "permanent": "true"})
    assert asked.status_code == 303
    assert asked.headers["location"].startswith("/reauth")
    assert not _locked(env, "docs")
    _reauth(client)
    done = client.post("/ui/files/lock", data={"path": "lib:docs", "permanent": "true"})
    assert done.status_code == 303
    assert "undo" not in _query(done.headers["location"])
    with env.runtime.dbs.state.read() as conn:
        settings = load_policy(conn).settings_for(FolderRef("lib", "docs"))
    assert settings.locked
    assert settings.permanent
    entry = env.runtime.executor.entries(limit=1)[0]
    assert entry.action == "folder.lock"
    assert not entry.undoable


def test_making_a_lock_permanent_from_the_locked_folder(client: TestClient, env: LockedEnv) -> None:
    form = client.get("/ui/locked/lock", params={"path": "lib:vault"})
    assert form.status_code == 200
    assert "permanent" in form.text
    assert client.post("/ui/locked/lock", data={"path": "lib:vault"}).status_code == 404
    done = client.post("/ui/locked/lock", data={"path": "lib:vault", "permanent": "true"})
    assert done.status_code == 303
    assert done.headers["location"].startswith("/ui/locked?path=lib:vault&did=permanent")
    page = client.get("/ui/locked", params={"path": "lib:vault"})
    assert "locked permanently" in page.text
    assert "Unlock…" not in page.text
    # A lock that comes from a parent can't be made permanent on its own.
    inner = client.post("/ui/locked/lock", data={"path": "lib:vault/inner", "permanent": "true"})
    assert inner.status_code == 409


def test_a_permanent_lock_cant_be_removed_from_the_ui_or_api(
    client: TestClient, env: LockedEnv
) -> None:
    with env.runtime.dbs.state.write() as conn:
        store.lock(conn, FolderRef("lib", "vault"), permanent=True, actor="test")
    preview = client.get("/ui/locked/unlock", params={"path": "lib:vault"})
    assert preview.status_code == 200
    assert "permanently locked" in preview.text
    assert 'action="/ui/locked/unlock"' not in preview.text
    refused = client.post("/ui/locked/unlock", data={"path": "lib:vault"})
    assert refused.status_code == 409
    api = client.post("/api/v1/actions/folder.unlock", json={"path": "lib:vault"})
    assert api.status_code in (400, 409), api.text
    settings_page = client.post(
        "/ui/settings/folders/lock", data={"folder": "lib:vault", "locked": "false"}
    )
    assert settings_page.status_code == 400
    with pytest.raises(ActionRefused, match="permanently"):
        env.runtime.executor.run(
            "folder.unlock", {"path": "lib:vault"}, actor="test", confirmed=True
        )
    assert _locked(env, "vault")
    # The host can.
    with env.runtime.dbs.state.write() as conn:
        with pytest.raises(PermanentLockError):
            store.unlock(conn, FolderRef("lib", "vault"), actor="test")
        store.unlock(conn, FolderRef("lib", "vault"), allow_permanent=True, actor="cli")
    assert not _locked(env, "vault")


def test_unlock_shows_what_becomes_visible(client: TestClient, env: LockedEnv) -> None:
    preview = client.get("/ui/locked/unlock", params={"path": "lib:vault"})
    assert preview.status_code == 200
    text = preview.text
    # vault/zebra-secret.txt, vault/inner/okapi-hidden.txt, vault/inner/deeper/note.txt;
    # vault/own keeps its own lock.
    assert "<strong>3</strong> files" in text
    assert "<strong>2</strong> folders" in text
    assert "lib:vault/inner/deeper/note.txt" in text
    assert "lib:vault/own/kept.txt" not in text
    assert "<code>lib:vault/own</code>" in text
    inner = client.get("/ui/locked/unlock", params={"path": "lib:vault/inner"})
    assert "because lib:vault is" in inner.text
    private = client.get("/ui/locked/unlock", params={"path": "lib:private"})
    assert "Its folder type locks it" in private.text


def test_unlock_needs_the_password_then_shows_the_folder_in_files(
    client: TestClient, env: LockedEnv
) -> None:
    env.runtime.auth.set_settings_from_host({"reauth_minutes": 0}, actor="test")
    env.forget_reauth()
    _reauth(client)
    client.get("/ui/locked/unlock", params={"path": "lib:vault"})
    first = client.post("/ui/locked/unlock", data={"path": "lib:vault"})
    assert first.status_code == 303
    assert first.headers["location"].startswith("/ui/files?path=lib:vault&did=unlock")
    assert not _locked(env, "vault")
    assert _locked(env, "vault/own")
    page = client.get("/ui/files", params={"path": "lib:vault"})
    assert "zebra-secret.txt" in page.text
    assert "own/" not in page.text


def test_unlocking_a_private_type_folder_overrides_its_type(
    client: TestClient, env: LockedEnv
) -> None:
    done = client.post("/ui/locked/unlock", data={"path": "lib:private"})
    assert done.status_code == 303
    assert not _locked(env, "private")
    assert "diary.txt" in client.get("/ui/files", params={"path": "lib:private"}).text


def test_the_executor_makes_permanent_locks_sensitive(env: LockedEnv) -> None:
    executor = env.runtime.executor
    assert not executor.preview("folder.lock", {"path": "lib:docs"}).sensitive
    assert executor.preview("folder.lock", {"path": "lib:docs", "permanent": True}).sensitive
    with pytest.raises(SensitiveActionError):
        executor.run("folder.lock", {"path": "lib:docs", "permanent": True}, actor="test")
    with pytest.raises(ActionRefused, match="already locked"):
        executor.run("folder.lock", {"path": "lib:vault"}, actor="test")
