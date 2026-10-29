# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Resumable uploads: the service (`core.executor.uploads`), the `file.upload` action
and `/api/v1/uploads`, including resuming after a dropped connection."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from api_support import PASSWORD, form_token, log_in, make_owner
from fastapi.testclient import TestClient
from support import make_runtime, use_fast_hashes

from steward.api import create_app
from steward.core.config import DiscoverySettings
from steward.core.db import utcnow
from steward.core.discovery import Scanner
from steward.core.executor import SensitiveActionError
from steward.core.executor.guarded import UPLOAD_PREFIX
from steward.core.executor.uploads import (
    OffsetMismatch,
    UploadConflict,
    UploadNotFound,
    Uploads,
)
from steward.core.folders import FolderRef, store
from steward.core.runtime import Runtime
from steward.modules.files_ui import FilesUiModule
from steward.sdk import ActionRefused

ACTOR = "user:1"
CHUNK = 1024 * 1024  # uploads.chunk_mib: 1 in these tests


@pytest.fixture(autouse=True)
def fast_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fast_hashes(monkeypatch)


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    rt = make_runtime(
        tmp_path, (FilesUiModule,), steward={"uploads": {"chunk_mib": 1, "stale_hours": 2}}
    )
    root = tmp_path / "lib"
    for relpath in ("docs/report.txt", "private/diary.txt", "apps/data.db"):
        (root / relpath).parent.mkdir(parents=True, exist_ok=True)
        (root / relpath).write_text(relpath)
    with rt.dbs.state.write() as conn:
        store.upsert_library(conn, "lib", root=str(root), actor=ACTOR)
        store.set_type(conn, FolderRef("lib", "private"), "private", actor=ACTOR)
        store.set_type(conn, FolderRef("lib", "apps"), "app-managed", actor=ACTOR)
        store.ensure_default_roles(conn, actor=ACTOR)
    yield rt
    rt.close()


def _root(runtime: Runtime) -> Path:
    from steward.core.folders import load_policy

    with runtime.dbs.state.read() as conn:
        return Path(load_policy(conn).libraries["lib"].root)


def _temps(folder: Path) -> list[str]:
    return [n for n in os.listdir(folder) if n.startswith(UPLOAD_PREFIX)]


def _data(n: int) -> bytes:
    return bytes(i % 251 for i in range(n))


# --- the service ---------------------------------------------------------------------------


def test_upload_in_pieces_then_finish(runtime: Runtime) -> None:
    uploads = runtime.uploads
    data = _data(CHUNK * 2 + 123)
    info = uploads.start("lib:docs", "big.bin", len(data), actor=ACTOR)
    assert info.received == 0
    assert _temps(_root(runtime) / "docs") == [UPLOAD_PREFIX + info.upload_id]
    offset = 0
    while offset < len(data):
        info = uploads.write(info.upload_id, offset, data[offset : offset + CHUNK], actor=ACTOR)
        offset = info.received
    entry = uploads.finish(info.upload_id, actor=ACTOR)
    assert entry.action == "file.upload"
    assert (_root(runtime) / "docs/big.bin").read_bytes() == data
    assert _temps(_root(runtime) / "docs") == []
    assert uploads.get(info.upload_id, actor=ACTOR).status == "finished"
    # Undo moves it to the trash.
    runtime.executor.undo(entry.entry_id, actor=ACTOR, confirmed=True)  # trash = sensitive
    assert not (_root(runtime) / "docs/big.bin").exists()
    assert list((_root(runtime) / ".steward/trash").rglob("big.bin"))


def test_a_lost_chunk_is_resumed_from_the_servers_offset(runtime: Runtime) -> None:
    uploads = runtime.uploads
    data = _data(CHUNK * 3)
    info = uploads.start("lib:docs", "resume.bin", len(data), actor=ACTOR)
    uploads.write(info.upload_id, 0, data[:CHUNK], actor=ACTOR)
    # The client thinks it sent two chunks; the second never arrived.
    with pytest.raises(OffsetMismatch) as caught:
        uploads.write(info.upload_id, CHUNK * 2, data[CHUNK * 2 :], actor=ACTOR)
    assert caught.value.offset == CHUNK
    # A crash after writing but before recording leaves extra bytes: they're cut back.
    temp = _root(runtime) / "docs" / (UPLOAD_PREFIX + info.upload_id)
    with temp.open("ab") as f:
        f.write(b"garbage from a crash")
    offset = uploads.get(info.upload_id, actor=ACTOR).received
    while offset < len(data):
        offset = uploads.write(
            info.upload_id, offset, data[offset : offset + CHUNK], actor=ACTOR
        ).received
    uploads.finish(info.upload_id, actor=ACTOR)
    assert (_root(runtime) / "docs/resume.bin").read_bytes() == data


def test_never_overwrites_and_offers_a_free_name(runtime: Runtime) -> None:
    uploads = runtime.uploads
    with pytest.raises(UploadConflict) as caught:
        uploads.start("lib:docs", "report.txt", 3, actor=ACTOR)
    assert caught.value.suggestion == "report (2).txt"
    info = uploads.start("lib:docs", "late.txt", 3, actor=ACTOR)
    uploads.write(info.upload_id, 0, b"new", actor=ACTOR)
    (_root(runtime) / "docs/late.txt").write_text("someone else")
    with pytest.raises(UploadConflict) as late:
        uploads.finish(info.upload_id, actor=ACTOR)
    assert late.value.suggestion == "late (2).txt"
    assert (_root(runtime) / "docs/late.txt").read_text() == "someone else"
    uploads.finish(info.upload_id, actor=ACTOR, name=late.value.suggestion)
    assert (_root(runtime) / "docs/late (2).txt").read_bytes() == b"new"


def test_unfinished_uploads_cant_be_finished(runtime: Runtime) -> None:
    uploads = runtime.uploads
    info = uploads.start("lib:docs", "half.bin", 10, actor=ACTOR)
    uploads.write(info.upload_id, 0, b"12345", actor=ACTOR)
    with pytest.raises(ActionRefused, match="still missing"):
        uploads.finish(info.upload_id, actor=ACTOR)
    with pytest.raises(ActionRefused, match="more bytes"):
        uploads.write(info.upload_id, 5, b"123456", actor=ACTOR)


@pytest.mark.parametrize(
    ("folder", "name"),
    [
        ("lib:apps", "x.txt"),  # app-managed: read-only
        ("lib:nope", "x.txt"),
        ("lib:docs/report.txt", "x.txt"),
        ("lib:docs", "../x.txt"),
        ("lib:docs", "a/b"),
        ("lib:docs", ""),
        ("lib:docs", ".steward-tmp-up-0123"),
        ("lib:../outside", "x.txt"),
        ("other:", "x.txt"),
    ],
)
def test_bad_destinations_are_refused(runtime: Runtime, folder: str, name: str) -> None:
    with pytest.raises(ActionRefused):
        runtime.uploads.start(folder, name, 1, actor=ACTOR)
    assert _temps(_root(runtime) / "docs") == []


def test_a_symlinked_folder_is_never_followed(runtime: Runtime, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (_root(runtime) / "docs/link").symlink_to(outside)
    with pytest.raises(ActionRefused):
        runtime.uploads.start("lib:docs/link", "x.txt", 1, actor=ACTOR)
    assert list(outside.iterdir()) == []


def test_locked_folders_need_a_confirmed_start(runtime: Runtime) -> None:
    with pytest.raises(SensitiveActionError):
        runtime.uploads.start("lib:private", "x.txt", 2, actor=ACTOR)
    info = runtime.uploads.start("lib:private", "x.txt", 2, actor=ACTOR, confirmed=True)
    runtime.uploads.write(info.upload_id, 0, b"hi", actor=ACTOR)
    runtime.uploads.finish(info.upload_id, actor=ACTOR)
    path = _root(runtime) / "private/x.txt"
    assert path.read_bytes() == b"hi"
    assert path.stat().st_mode & 0o777 == 0o600


def test_free_space_and_size_limits(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    class Tiny:
        f_bavail = 10
        f_frsize = 4096

    monkeypatch.setattr(os, "fstatvfs", lambda fd: Tiny())
    with pytest.raises(ActionRefused, match="free space"):
        runtime.uploads.start("lib:docs", "x.bin", 10, actor=ACTOR)
    monkeypatch.undo()
    limited = Uploads(
        runtime.dbs,
        runtime.executor,
        runtime.config.steward.uploads.model_copy(update={"max_file_gib": 0.000001}),
    )
    with pytest.raises(ActionRefused, match="aren't accepted"):
        limited.start("lib:docs", "x.bin", 10_000, actor=ACTOR)


def test_cancel_and_expire_remove_the_temp_file(runtime: Runtime) -> None:
    uploads = runtime.uploads
    a = uploads.start("lib:docs", "a.bin", 4, actor=ACTOR)
    uploads.cancel(a.upload_id, actor=ACTOR)
    assert uploads.get(a.upload_id, actor=ACTOR).status == "cancelled"
    with pytest.raises(UploadNotFound):
        uploads.write(a.upload_id, 0, b"x", actor=ACTOR)
    b = uploads.start("lib:docs", "b.bin", 4, actor=ACTOR)
    assert runtime.uploads.expire() == 0
    later = Uploads(
        runtime.dbs,
        runtime.executor,
        runtime.config.steward.uploads,
        clock=lambda: utcnow() + timedelta(hours=3),
    )
    assert later.expire() == 1
    assert uploads.get(b.upload_id, actor=ACTOR).status == "expired"
    assert _temps(_root(runtime) / "docs") == []


def test_uploads_belong_to_who_started_them(runtime: Runtime) -> None:
    info = runtime.uploads.start("lib:docs", "mine.bin", 1, actor=ACTOR)
    with pytest.raises(UploadNotFound):
        runtime.uploads.write(info.upload_id, 0, b"x", actor="user:2")


def test_discovery_ignores_unfinished_uploads(runtime: Runtime) -> None:
    info = runtime.uploads.start("lib:docs", "wip.bin", 4, actor=ACTOR)
    runtime.uploads.write(info.upload_id, 0, b"ab", actor=ACTOR)
    Scanner(runtime.dbs, DiscoverySettings()).scan("lib")
    with runtime.dbs.state.read() as conn:
        from sqlalchemy import select

        from steward.core.db.schema import files

        names = [r.relpath for r in conn.execute(select(files.c.relpath))]
    assert not any(UPLOAD_PREFIX in n for n in names)


@pytest.mark.parametrize(
    "params",
    [
        {"temp": "lib:docs/report.txt", "path": "lib:docs/x.txt", "size": 15},
        {"temp": f"lib:{UPLOAD_PREFIX}{'0' * 32}", "path": "lib:docs/x.txt", "size": 0},
        {"temp": f"lib:docs/{UPLOAD_PREFIX}{'0' * 32}", "path": "lib:docs/x.txt", "size": 0},
    ],
)
def test_file_upload_only_moves_a_complete_upload_next_to_it(
    runtime: Runtime, params: dict[str, Any]
) -> None:
    with pytest.raises(ActionRefused):
        runtime.executor.run("file.upload", params, actor=ACTOR)
    assert (_root(runtime) / "docs/report.txt").exists()


# --- HTTP ----------------------------------------------------------------------------------


@pytest.fixture
def client(runtime: Runtime) -> Iterator[TestClient]:
    make_owner(runtime)
    Scanner(runtime.dbs, DiscoverySettings()).scan("lib")
    with TestClient(create_app(runtime), follow_redirects=False) as c:
        c.headers["X-CSRF-Token"] = log_in(c)
        yield c


OCTETS = {"Content-Type": "application/offset+octet-stream"}


def _put(client: TestClient, upload_id: str, offset: int, data: bytes) -> Any:
    return client.put(
        f"/api/v1/uploads/{upload_id}",
        content=data,
        headers={**OCTETS, "Upload-Offset": str(offset)},
    )


def test_http_upload_with_a_dropped_connection(client: TestClient, runtime: Runtime) -> None:
    data = _data(CHUNK * 2 + 10)
    started = client.post(
        "/api/v1/uploads", json={"folder": "lib:docs", "name": "film.mp4", "size": len(data)}
    )
    assert started.status_code == 201, started.text
    body = started.json()
    assert body["chunk_bytes"] == CHUNK
    upload_id = body["upload_id"]
    assert _put(client, upload_id, 0, data[:CHUNK]).json()["offset"] == CHUNK
    # The next chunk is cut off by a dropped connection: the server keeps nothing of it.
    assert _put(client, upload_id, CHUNK, data[CHUNK : CHUNK + 100]).status_code == 200
    # ...so a real client asks where to carry on (here 100 bytes did arrive in full).
    offset = client.get(f"/api/v1/uploads/{upload_id}").json()["offset"]
    assert offset == CHUNK + 100
    wrong = _put(client, upload_id, 0, data[:10])
    assert wrong.status_code == 409
    assert wrong.json()["offset"] == offset
    while offset < len(data):
        offset = _put(client, upload_id, offset, data[offset : offset + CHUNK]).json()["offset"]
    listed = client.get("/api/v1/uploads").json()["uploads"]
    assert [u["upload_id"] for u in listed] == [upload_id]
    done = client.post(f"/api/v1/uploads/{upload_id}/finish", json={})
    assert done.status_code == 201, done.text
    assert done.json()["path"] == "lib:docs/film.mp4"
    assert (_root(runtime) / "docs/film.mp4").read_bytes() == data
    assert client.get("/api/v1/uploads").json()["uploads"] == []
    page = client.get("/ui/files", params={"path": "lib:docs"})
    assert "film.mp4" in page.text
    assert UPLOAD_PREFIX not in page.text


def test_http_refusals(client: TestClient) -> None:
    upload_id = client.post(
        "/api/v1/uploads", json={"folder": "lib:docs", "name": "x.bin", "size": CHUNK * 3}
    ).json()["upload_id"]
    too_big = _put(client, upload_id, 0, b"x" * (CHUNK + 1))
    assert too_big.status_code == 413
    no_type = client.put(
        f"/api/v1/uploads/{upload_id}", content=b"x", headers={"Upload-Offset": "0"}
    )
    assert no_type.status_code == 415
    no_offset = client.put(f"/api/v1/uploads/{upload_id}", content=b"x", headers=OCTETS)
    assert no_offset.status_code == 400
    clash = client.post(
        "/api/v1/uploads", json={"folder": "lib:docs", "name": "report.txt", "size": 1}
    )
    assert clash.status_code == 409
    assert clash.json()["suggestion"] == "report (2).txt"
    assert client.get("/api/v1/uploads/nope").status_code == 404
    assert client.delete(f"/api/v1/uploads/{upload_id}").status_code == 204
    assert _put(client, upload_id, 0, b"x").status_code == 404


def test_http_locked_folders_are_invisible_in_the_normal_scope(
    client: TestClient, runtime: Runtime
) -> None:
    refused = client.post(
        "/api/v1/uploads", json={"folder": "lib:private", "name": "x.txt", "size": 1}
    )
    assert refused.status_code == 404
    # The locked scope works after a recent password check (the login counts).
    ok = client.post(
        "/api/v1/uploads",
        json={"folder": "lib:private", "name": "x.txt", "size": 1, "scope": "locked"},
    )
    assert ok.status_code == 201, ok.text
    assert client.get("/api/v1/uploads").json()["uploads"] == []
    locked = client.get("/api/v1/uploads", params={"scope": "locked"}).json()["uploads"]
    assert [u["name"] for u in locked] == ["x.txt"]


def test_http_needs_the_csrf_token_and_a_session(client: TestClient, runtime: Runtime) -> None:
    body = {"folder": "lib:docs", "name": "x.bin", "size": 1}
    no_csrf = client.post("/api/v1/uploads", json=body, headers={"X-CSRF-Token": ""})
    assert no_csrf.status_code == 403
    from api_support import USERNAME

    user = runtime.auth.get_user(username=USERNAME)
    _, secret = runtime.auth.issue_token(user.user_id, "t", ["read", "propose"], actor="test")
    with TestClient(create_app(runtime), follow_redirects=False) as anon:
        token = anon.post(
            "/api/v1/uploads", json=body, headers={"Authorization": f"Bearer {secret}"}
        )
        assert token.status_code == 403


def test_the_folder_page_offers_uploads_only_where_writable(client: TestClient) -> None:
    docs = client.get("/ui/files", params={"path": "lib:docs"})
    assert 'id="uploads"' in docs.text
    assert "/static/m/files_ui/upload.js" in docs.text
    apps = client.get("/ui/files", params={"path": "lib:apps"})
    assert 'id="uploads"' not in apps.text
    script = client.get("/static/m/files_ui/upload.js")
    assert script.status_code == 200
    assert "Upload-Offset" in script.text


def test_reauth_then_locked_upload_with_window_zero(client: TestClient, runtime: Runtime) -> None:
    runtime.auth.set_settings_from_host({"reauth_minutes": 0}, actor="test")
    from sqlalchemy import update

    from steward.core.db.schema import sessions

    with runtime.dbs.state.write() as conn:
        conn.execute(update(sessions).values(reauth_at=None))
    body = {"folder": "lib:private", "name": "y.txt", "size": 1, "scope": "locked"}
    assert client.post("/api/v1/uploads", json=body).status_code == 403
    page = client.get("/reauth")
    client.post("/reauth", data={"password": PASSWORD, "csrf_token": form_token(page.text)})
    started = client.post("/api/v1/uploads", json=body)
    assert started.status_code == 201
    upload_id = started.json()["upload_id"]
    _put(client, upload_id, 0, b"z")
    # Finishing doesn't ask again: the start was confirmed.
    assert client.post(f"/api/v1/uploads/{upload_id}/finish").status_code == 201
