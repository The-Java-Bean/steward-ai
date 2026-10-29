# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The `shares` module end to end (plan §14.4, Phase 2 acceptance): the sharing rules,
the public `/s/<token>` pages (passwords, files, folders, zips, the access log),
expiry, revocation, undo, `/ui/shares`, and that a link never serves anything that may
not be shared, even when the item changes after it was shared."""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from api_support import PASSWORD, USERNAME, form_token, log_in
from fastapi.testclient import TestClient
from locked_support import SECRET_CONTENT, LockedEnv
from sqlalchemy import select, update
from support import make_module, make_runtime, use_fast_hashes

from steward.api import create_app
from steward.api.app import UiPageError
from steward.core.db.schema import files, journal
from steward.core.folders import FolderRef, store
from steward.core.folders.types import TypeDefinition
from steward.core.privacy import set_override
from steward.modules.files_ui import FilesUiModule
from steward.modules.shares import SharesModule, expire
from steward.modules.shares import store as shares
from steward.modules.shares.public import MAX_FAILURES_PER_ADDRESS
from steward.sdk import ActionRefused, JobContext, Registry

ACTOR = f"user:{USERNAME}"
SHARE_PASSWORD = "share-pass-1234"


class ShareEnv(LockedEnv):
    """`LockedEnv` plus things inside `docs` that may not be shared: an excluded secret
    (`*.key`), a file marked local-only, a locked subfolder and a folder whose type
    doesn't allow sharing."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        for relpath, content in {
            "docs/server.key": "KEY-MATERIAL",
            "docs/notes-local.txt": "local only notes",
            "docs/closed/inside.txt": f"{SECRET_CONTENT} closed",
            "docs/noshare/kept.txt": "not for sharing",
            "docs/photo.png": "\x89PNG\r\n\x1a\nfake",
            ".steward/trash/old.txt": "trashed",
        }.items():
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("latin-1"))
        self.scan()
        with self.runtime.dbs.state.write() as conn:
            store.lock(conn, FolderRef("lib", "docs/closed"), actor=ACTOR)
            store.define_type(
                conn, "noshare", TypeDefinition(extends="documents", shareable=False), actor=ACTOR
            )
            store.set_type(conn, FolderRef("lib", "docs/noshare"), "noshare", actor=ACTOR)
            file_id = conn.execute(
                select(files.c.file_id).where(files.c.relpath == "docs/notes-local.txt")
            ).scalar_one()
            set_override(conn, file_id, "local-only", actor=ACTOR)

    def create(self, path: str, **params: Any) -> shares.Share:
        params.setdefault("expires_at", (datetime.now(UTC) + timedelta(days=7)).isoformat())
        entry = self.runtime.executor.run(
            "shares.create", {"path": path, **params}, actor=ACTOR, confirmed=True
        )
        with self.runtime.dbs.state.read() as conn:
            found = shares.get(conn, entry.result["share_id"])
        assert found is not None
        return found

    def share(self, share_id: str) -> shares.Share:
        with self.runtime.dbs.state.read() as conn:
            found = shares.get(conn, share_id)
        assert found is not None
        return found


@pytest.fixture(autouse=True)
def fast_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fast_hashes(monkeypatch)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[ShareEnv]:
    e = ShareEnv(tmp_path)
    yield e
    e.runtime.close()


@pytest.fixture
def owner(env: ShareEnv) -> Iterator[TestClient]:
    with TestClient(create_app(env.runtime), follow_redirects=False) as client:
        client.headers["X-CSRF-Token"] = log_in(client)
        yield client


@pytest.fixture
def visitor(env: ShareEnv) -> Iterator[TestClient]:
    with TestClient(create_app(env.runtime), follow_redirects=False) as client:
        yield client


def _unlock_with_password(visitor: TestClient, url: str, password: str = SHARE_PASSWORD) -> Any:
    page = visitor.get(url)
    assert page.status_code == 401
    return visitor.post(url, data={"password": password, "csrf_token": form_token(page.text)})


# --- the rules --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("lib:", "whole library"),
        ("lib:docs/missing.txt", "nothing at"),
        ("lib:vault/zebra-secret.txt", "locked folder"),
        ("lib:private/diary.txt", "locked folder"),
        ("lib:docs/closed", "locked folder"),
        ("lib:docs/server.key", "excluded"),
        ("lib:docs/notes-local.txt", "local-only"),
        ("lib:docs/noshare", "noshare"),
        ("lib:docs/noshare/kept.txt", "noshare"),
        ("lib:.steward/trash", "hidden"),
    ],
)
def test_shares_are_refused_where_the_rules_say_so(env: ShareEnv, path: str, message: str) -> None:
    with pytest.raises(ActionRefused, match=message):
        env.create(path)


def test_refused_parameters(env: ShareEnv) -> None:
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    with pytest.raises(ActionRefused, match="past"):
        env.create("lib:docs/report.txt", expires_at=past)
    with pytest.raises(ActionRefused, match="snapshot"):
        env.create("lib:docs/report.txt", snapshot=True)
    with pytest.raises(ActionRefused, match="password"):
        env.create("lib:docs/report.txt", password="short")
    with pytest.raises(ActionRefused, match="time zone"):
        env.create("lib:docs/report.txt", expires_at="2099-01-01T00:00:00")


def test_no_expiry_is_sensitive_and_secrets_never_reach_the_journal(env: ShareEnv) -> None:
    preview = env.runtime.executor.preview(
        "shares.create", {"path": "lib:docs/report.txt", "expires_at": None}
    )
    assert preview.sensitive
    assert any("never expires" in d for d in preview.rendered.details)
    with pytest.raises(ActionRefused, match="password"):
        env.runtime.executor.run(
            "shares.create", {"path": "lib:docs/report.txt", "expires_at": None}, actor=ACTOR
        )
    share = env.create("lib:docs/report.txt", expires_at=None, password=SHARE_PASSWORD)
    assert share.no_expiry
    assert share.has_password
    with env.runtime.dbs.state.read() as conn:
        rows = [dict(r._mapping) for r in conn.execute(select(journal))]
    text = json.dumps(rows, default=str)
    assert SHARE_PASSWORD not in text
    assert share.token not in text
    assert share.password_hash is not None
    assert share.password_hash not in text


# --- public pages -----------------------------------------------------------------------


def test_a_file_link_with_a_password(env: ShareEnv, visitor: TestClient) -> None:
    share = env.create("lib:docs/report.txt", password=SHARE_PASSWORD)
    url = f"/s/{share.token}"
    page = visitor.get(url)
    assert page.status_code == 401
    assert "report.txt" not in page.text  # nothing about the item before the password
    wrong = visitor.post(
        url, data={"password": "nope-nope-nope", "csrf_token": form_token(page.text)}
    )
    assert wrong.status_code == 403
    right = _unlock_with_password(visitor, url)
    assert right.status_code == 303
    assert right.headers["location"] == url
    cookie = right.headers["set-cookie"]
    assert f"Path={url}" in cookie
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()
    shown = visitor.get(url)
    assert shown.status_code == 200
    assert "report.txt" in shown.text
    assert "quarterly report" in shown.text  # text preview
    assert shown.headers["referrer-policy"] == "no-referrer"
    assert "noindex" in shown.headers["x-robots-tag"]
    assert "no-store" in shown.headers["cache-control"]
    raw = visitor.get(url, params={"raw": "1"})
    assert raw.content == b"quarterly report\n"
    assert raw.headers["content-disposition"].startswith("attachment")
    assert "sandbox" in raw.headers["content-security-policy"]
    assert visitor.get(f"{url}/anything").status_code == 404  # a file share has no inside
    kinds = [a.kind for a in _log(env, share.share_id)]
    assert kinds.count("password_failed") == 1
    assert "view" in kinds
    assert "download" in kinds
    assert env.share(share.share_id).access_count == 2  # the page and the download


def test_a_new_password_ends_earlier_grants(env: ShareEnv, visitor: TestClient) -> None:
    share = env.create("lib:docs/report.txt", password=SHARE_PASSWORD)
    url = f"/s/{share.token}"
    _unlock_with_password(visitor, url)
    assert visitor.get(url).status_code == 200
    env.runtime.executor.run(
        "shares.update",
        {"share_id": share.share_id, "password_change": "set", "new_password": "another-pass-9"},
        actor=ACTOR,
    )
    assert visitor.get(url).status_code == 401
    assert _unlock_with_password(visitor, url).status_code == 403
    assert _unlock_with_password(visitor, url, "another-pass-9").status_code == 303


def test_wrong_passwords_are_slowed_down(env: ShareEnv, visitor: TestClient) -> None:
    share = env.create("lib:docs/report.txt", password=SHARE_PASSWORD)
    url = f"/s/{share.token}"
    for _ in range(MAX_FAILURES_PER_ADDRESS):
        assert _unlock_with_password(visitor, url, "wrong-password").status_code == 403
    limited = _unlock_with_password(visitor, url)  # even the right one, for a while
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "900"


def test_a_folder_link_leaves_out_what_may_not_be_shared(
    env: ShareEnv, visitor: TestClient
) -> None:
    share = env.create("lib:docs")
    url = f"/s/{share.token}"
    page = visitor.get(url)
    assert page.status_code == 200
    for shown in ("report.txt", "sub/", "photo.png"):
        assert shown in page.text
    for hidden in ("server.key", "notes-local.txt", "closed", "noshare"):
        assert hidden not in page.text
    assert "lib:" not in page.text  # public pages never show library paths
    deep = visitor.get(f"{url}/sub")
    assert deep.status_code == 200
    assert "deep.txt" in deep.text
    assert visitor.get(f"{url}/sub/deep.txt", params={"raw": "1"}).content == b"deep\n"
    image = visitor.get(f"{url}/photo.png", params={"raw": "1", "inline": "1"})
    assert image.headers["content-disposition"].startswith("inline")
    for refused in (
        "server.key",
        "notes-local.txt",
        "closed/inside.txt",
        "closed",
        "noshare/kept.txt",
        "../vault/zebra-secret.txt",
        "..",
        "sub/../../vault",
        "%2e%2e/vault",
    ):
        response = visitor.get(f"{url}/{refused}", params={"raw": "1"})
        assert response.status_code == 404, refused
        assert SECRET_CONTENT not in response.text
    archive = visitor.get(url, params={"zip": "1"})
    names = zipfile.ZipFile(io.BytesIO(archive.content)).namelist()
    assert "docs/report.txt" in names
    assert "docs/sub/deep.txt" in names
    assert not [n for n in names if re.search(r"server\.key|notes-local|closed|noshare", n)]


def test_a_snapshot_leaves_out_files_added_later(env: ShareEnv, visitor: TestClient) -> None:
    share = env.create("lib:docs/sub", snapshot=True)
    (env.root / "docs/sub/later.txt").write_text("added later\n")
    env.scan()
    url = f"/s/{share.token}"
    page = visitor.get(url)
    assert "deep.txt" in page.text
    assert "later.txt" not in page.text
    assert visitor.get(f"{url}/later.txt", params={"raw": "1"}).status_code == 404
    names = zipfile.ZipFile(io.BytesIO(visitor.get(url, params={"zip": "1"}).content)).namelist()
    assert names == ["sub/", "sub/deep.txt"]


def test_locking_the_item_after_sharing_stops_the_link(
    env: ShareEnv, visitor: TestClient, owner: TestClient
) -> None:
    share = env.create("lib:docs/sub/deep.txt")
    url = f"/s/{share.token}"
    assert visitor.get(url).status_code == 200
    with env.runtime.dbs.state.write() as conn:
        store.lock(conn, FolderRef("lib", "docs/sub"), actor=ACTOR)
    gone = visitor.get(url)
    assert gone.status_code == 404
    assert "deep.txt" not in gone.text
    listed = owner.get("/ui/shares")
    assert "deep.txt" not in listed.text  # the item is in a locked folder now: no name
    assert "An item in a locked or hidden folder" in listed.text
    assert "locked or hidden folder now" in listed.text
    detail = owner.get(f"/ui/shares/{share.share_id}")
    assert "deep.txt" not in detail.text
    assert owner.get("/ui/shares", params={"q": "deep"}).text.count("Details") == 0


def test_unknown_expired_and_revoked_links_look_the_same(
    env: ShareEnv, visitor: TestClient
) -> None:
    revoked = env.create("lib:docs/report.txt")
    env.runtime.executor.run("shares.revoke", {"share_id": revoked.share_id}, actor=ACTOR)
    expired = env.create("lib:docs/report.txt")
    with env.runtime.dbs.state.write() as conn:
        conn.execute(
            update(shares.links)
            .where(shares.links.c.share_id == expired.share_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    pages = [
        visitor.get(f"/s/{token}")
        for token in (revoked.token, expired.token, "A" * 22, "short", "x" * 23)
    ]
    assert {p.status_code for p in pages} == {404}
    assert len({p.text for p in pages}) == 1


def test_expiry_job_records_the_end(env: ShareEnv) -> None:
    share = env.create("lib:docs/report.txt")
    stamp = datetime.now(UTC) - timedelta(minutes=5)
    with env.runtime.dbs.state.write() as conn:
        conn.execute(
            update(shares.links)
            .where(shares.links.c.share_id == share.share_id)
            .values(expires_at=stamp)
        )
    ctx = JobContext(1, "shares.expire", {}, 1, lambda: None, env.runtime.dbs.state)
    expire(ctx)
    ended = env.share(share.share_id)
    assert ended.end_reason == "expired"
    assert ended.ended_at == stamp
    assert "shares.expire" in env.runtime.schedules


def test_undo_turns_the_link_off_and_revoking_is_final(env: ShareEnv) -> None:
    executor = env.runtime.executor
    share = env.create("lib:docs/report.txt")
    created = executor.entries(limit=1)[0]
    executor.undo(created.entry_id, actor=ACTOR)
    assert env.share(share.share_id).end_reason == "undone"
    with pytest.raises(ActionRefused, match="already ended"):
        executor.run("shares.revoke", {"share_id": share.share_id}, actor=ACTOR)
    other = env.create("lib:docs/report.txt")
    revoke = executor.run("shares.revoke", {"share_id": other.share_id}, actor=ACTOR)
    assert not revoke.undoable


def test_changing_the_expiry_can_be_undone(env: ShareEnv) -> None:
    executor = env.runtime.executor
    share = env.create("lib:docs/report.txt")
    later = datetime.now(UTC) + timedelta(days=30)
    entry = executor.run(
        "shares.update",
        {"share_id": share.share_id, "expiry": "set", "expires_at": later.isoformat()},
        actor=ACTOR,
    )
    assert env.share(share.share_id).expires_at == later
    executor.undo(entry.entry_id, actor=ACTOR)
    assert env.share(share.share_id).expires_at == share.expires_at
    never = executor.preview("shares.update", {"share_id": share.share_id, "expiry": "never"})
    assert never.sensitive
    with pytest.raises(ActionRefused, match="nothing to change"):
        executor.run("shares.update", {"share_id": share.share_id}, actor=ACTOR)


# --- the owner's pages ------------------------------------------------------------------


def test_sharing_from_the_dialog(env: ShareEnv, owner: TestClient, visitor: TestClient) -> None:
    files_page = owner.get("/ui/files", params={"path": "lib:docs"})
    assert "/ui/shares/new?path=lib%3Adocs/report.txt" in files_page.text
    dialog = owner.get("/ui/shares/new", params={"path": "lib:docs"})
    assert dialog.status_code == 200
    assert "Files added to this folder later are shared too" in dialog.text
    created = owner.post(
        "/ui/shares/new",
        data={"path": "lib:docs", "expiry": "7", "password_on": "1", "password": SHARE_PASSWORD},
    )
    assert created.status_code == 200
    assert SHARE_PASSWORD in created.text
    link = re.search(r'value="(http://testserver/s/[A-Za-z0-9_-]{22})"', created.text)
    assert link is not None
    token = link.group(1).rsplit("/", 1)[1]
    assert _unlock_with_password(visitor, f"/s/{token}").status_code == 303
    listed = owner.get("/ui/shares")
    assert "lib:docs" in listed.text
    assert token in listed.text
    assert SHARE_PASSWORD not in listed.text


def test_dialog_explains_refusals(env: ShareEnv, owner: TestClient) -> None:
    page = owner.get("/ui/shares/new", params={"path": "lib:docs/notes-local.txt"})
    assert "be shared. It is marked local-only" in page.text
    assert "Create link" not in page.text
    assert owner.get("/ui/shares/new", params={"path": "lib:vault"}).status_code == 404
    assert owner.get("/ui/shares/new", params={"path": "lib:docs/nope"}).status_code == 404
    refused = owner.post(
        "/ui/shares/new", data={"path": "lib:docs/report.txt", "expiry": "custom", "days": "x"}
    )
    assert refused.status_code == 409
    assert "choose how long" in refused.text


def test_no_expiry_asks_for_the_password_and_shows_up(env: ShareEnv, owner: TestClient) -> None:
    env.forget_reauth()
    data = {"path": "lib:docs/report.txt", "expiry": "never"}
    referer = "http://testserver/ui/shares/new?path=lib:docs/report.txt"
    asked = owner.post("/ui/shares/new", data=data, headers={"Referer": referer})
    assert asked.status_code == 303
    assert asked.headers["location"].startswith("/reauth")
    with env.runtime.dbs.state.read() as conn:
        assert shares.listing(conn, now=datetime.now(UTC), past=False) == []
    page = owner.get("/reauth")
    owner.post("/reauth", data={"password": PASSWORD, "csrf_token": form_token(page.text)})
    done = owner.post("/ui/shares/new", data=data)
    assert done.status_code == 200
    assert "never expires" in done.text
    listed = owner.get("/ui/shares")
    assert "no expiry" in listed.text
    assert re.search(r'Shares <span class="badge"[^>]*>1</span>', listed.text)


def test_revoking_moves_a_share_to_past(
    env: ShareEnv, owner: TestClient, visitor: TestClient
) -> None:
    share = env.create("lib:docs/report.txt")
    assert visitor.get(f"/s/{share.token}").status_code == 200
    response = owner.post(f"/ui/shares/{share.share_id}/revoke")
    assert response.headers["location"] == f"/ui/shares/{share.share_id}?did=revoked"
    assert visitor.get(f"/s/{share.token}").status_code == 404
    assert share.token not in owner.get("/ui/shares").text
    past = owner.get("/ui/shares", params={"status": "past"})
    assert "lib:docs/report.txt" in past.text
    assert "revoked" in past.text
    detail = owner.get(f"/ui/shares/{share.share_id}")
    assert "opened" in detail.text  # the access log
    assert owner.get(f"/ui/shares/{'0' * 32}").status_code == 404


def test_editing_a_share(env: ShareEnv, owner: TestClient) -> None:
    share = env.create("lib:docs/report.txt")
    page = owner.get(f"/ui/shares/{share.share_id}/edit")
    assert page.status_code == 200
    saved = owner.post(
        f"/ui/shares/{share.share_id}/edit",
        data={"expiry": "30", "password_change": "set", "new_password": SHARE_PASSWORD},
    )
    assert saved.status_code == 303
    changed = env.share(share.share_id)
    assert changed.has_password
    assert changed.expires_at is not None
    assert changed.expires_at > datetime.now(UTC) + timedelta(days=29)


def test_the_locked_folder_offers_no_share_links(env: ShareEnv, owner: TestClient) -> None:
    page = owner.get("/ui/locked", params={"path": "lib:vault"})
    assert page.status_code == 200
    assert "/ui/shares/new" not in page.text


# --- module wiring ----------------------------------------------------------------------


def test_the_exporter_leaves_out_secrets(env: ShareEnv) -> None:
    share = env.create("lib:docs/report.txt", password=SHARE_PASSWORD)
    with env.runtime.dbs.state.read() as conn:
        exported = env.runtime.registrations.exporters()["shares"](conn)
    rows = exported["shares_links"]
    assert rows
    assert rows[0]["has_password"] is True
    text = json.dumps(exported, default=str)
    assert share.token not in text
    assert "password_hash" not in text


def test_without_the_module_links_are_404(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, (FilesUiModule,))
    try:
        with TestClient(create_app(runtime), follow_redirects=False) as client:
            assert client.get(f"/s/{'A' * 22}").status_code == 404
            page = client.get("/ui/files")
            assert "/ui/shares" not in page.text
    finally:
        runtime.close()


def test_only_one_module_may_answer_share_links(tmp_path: Path) -> None:
    def register(r: Registry) -> None:
        r.share_links(lambda share: share.not_found())

    runtime = make_runtime(tmp_path, (SharesModule, make_module("other", register=register)))
    try:
        with pytest.raises(UiPageError, match="only one module"):
            create_app(runtime)
    finally:
        runtime.close()


def _log(env: ShareEnv, share_id: str) -> list[shares.Access]:
    with env.runtime.dbs.state.read() as conn:
        return shares.accesses(conn, share_id)
