# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The `files_ui` module: browsing, previews, downloads and zips, and every change
journaled with undo, all through `steward.sdk.web`."""

from __future__ import annotations

import io
import os
import re
import zipfile
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from api_support import log_in, make_owner
from fastapi.testclient import TestClient
from support import use_fast_hashes

from steward.api import create_app
from steward.core.folders import FolderRef, store
from steward.modules.files_ui import FilesUiModule

ACTOR = "user:1"


@pytest.fixture(autouse=True)
def fast_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fast_hashes(monkeypatch)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        from support import make_runtime

        from steward.core.config import DiscoverySettings
        from steward.core.discovery import Scanner

        self.runtime = make_runtime(tmp_path, (FilesUiModule,))
        self.root = tmp_path / "lib"
        tree = {
            "docs/report.txt": "quarterly report\n",
            "docs/notes.md": "# notes\n",
            "docs/sub/deep.txt": "deep\n",
            "photos/pic.png": "\x89PNG\r\n\x1a\nfake",
            "media/clip.mp4": "0123456789" * 100,
            "page.html": "<script>alert(1)</script>",
            "private/diary.txt": "secret diary\n",
        }
        for relpath, content in tree.items():
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("latin-1"))
        (self.root / "docs/link.txt").symlink_to(self.root / "private/diary.txt")
        with self.runtime.dbs.state.write() as conn:
            store.upsert_library(conn, "lib", root=str(self.root), actor=ACTOR)
            store.set_type(conn, FolderRef("lib", "private"), "private", actor=ACTOR)
            store.ensure_default_roles(conn, actor=ACTOR)
        Scanner(self.runtime.dbs, DiscoverySettings()).scan("lib")
        make_owner(self.runtime)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    yield e
    e.runtime.close()


@pytest.fixture
def client(env: Env) -> Iterator[TestClient]:
    with TestClient(create_app(env.runtime), follow_redirects=False) as c:
        c.headers["X-CSRF-Token"] = log_in(c)
        yield c


def _undo_ids(response: httpx2.Response) -> str:
    location = response.headers["location"]
    return str(parse_qs(urlparse(location).query)["undo"][0])


# --- browsing -----------------------------------------------------------------------------


def test_the_files_page_is_in_the_nav_and_opens_the_only_library(client: TestClient) -> None:
    home = client.get("/ui/")
    assert 'href="/ui/files"' in home.text
    response = client.get("/ui/files")
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/files?path=lib:"


def test_a_folder_lists_folders_then_files_and_hides_locked_and_steward_folders(
    client: TestClient,
) -> None:
    page = client.get("/ui/files", params={"path": "lib:"})
    assert page.status_code == 200
    text = page.text
    assert "docs/" in text
    assert "photos/" in text
    assert "page.html" in text
    assert "private" not in text
    assert ".steward" not in text
    assert text.index("docs/") < text.index("page.html")
    assert 'href="/static/m/files_ui/files.css"' in text


def test_symlinks_are_never_listed_or_followed(client: TestClient) -> None:
    page = client.get("/ui/files", params={"path": "lib:docs"})
    assert "report.txt" in page.text
    assert "link.txt" not in page.text
    assert client.get("/ui/files/raw", params={"path": "lib:docs/link.txt"}).status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "lib:private",
        "lib:private/diary.txt",
        "lib:.steward",
        "lib:../etc",
        "lib:docs/../private",
        "nope:",
        "lib",
        "lib:/docs",
    ],
)
def test_locked_hidden_and_bad_paths_are_not_found(client: TestClient, path: str) -> None:
    for url in ("/ui/files", "/ui/files/view", "/ui/files/raw", "/ui/files/zip"):
        response = client.get(url, params={"path": path})
        assert response.status_code == 404, (url, path)
        assert "secret diary" not in response.text


def test_files_discovery_hasnt_seen_still_show(client: TestClient, env: Env) -> None:
    (env.root / "docs/new.txt").write_text("fresh")
    page = client.get("/ui/files", params={"path": "lib:docs"})
    assert "new.txt" in page.text


def test_browse_by_kind_lists_scanned_files(client: TestClient) -> None:
    page = client.get("/ui/files", params={"kind": "text"})
    assert page.status_code == 200
    assert "report.txt" in page.text
    assert "diary.txt" not in page.text
    assert client.get("/ui/files", params={"kind": "bogus"}).status_code == 404


# --- previews and downloads ----------------------------------------------------------------


def test_text_preview_and_download(client: TestClient) -> None:
    view = client.get("/ui/files/view", params={"path": "lib:docs/report.txt"})
    assert view.status_code == 200
    assert "quarterly report" in view.text
    raw = client.get("/ui/files/raw", params={"path": "lib:docs/report.txt"})
    assert raw.status_code == 200
    assert raw.content == b"quarterly report\n"
    assert raw.headers["content-disposition"].startswith("attachment;")
    assert "sandbox" in raw.headers["content-security-policy"]


def test_images_show_inline_but_html_never_does(client: TestClient) -> None:
    img = client.get("/ui/files/raw", params={"path": "lib:photos/pic.png", "inline": "1"})
    assert img.headers["content-type"] == "image/png"
    assert img.headers["content-disposition"].startswith("inline;")
    html = client.get("/ui/files/raw", params={"path": "lib:page.html", "inline": "1"})
    assert html.headers["content-disposition"].startswith("attachment;")
    assert "sandbox" in html.headers["content-security-policy"]
    assert html.headers["x-content-type-options"] == "nosniff"


def test_byte_ranges_for_media(client: TestClient) -> None:
    full = b"0123456789" * 100
    part = client.get(
        "/ui/files/raw", params={"path": "lib:media/clip.mp4"}, headers={"Range": "bytes=10-19"}
    )
    assert part.status_code == 206
    assert part.content == full[10:20]
    assert part.headers["content-range"] == "bytes 10-19/1000"
    tail = client.get(
        "/ui/files/raw", params={"path": "lib:media/clip.mp4"}, headers={"Range": "bytes=-5"}
    )
    assert tail.content == full[-5:]
    bad = client.get(
        "/ui/files/raw", params={"path": "lib:media/clip.mp4"}, headers={"Range": "bytes=5000-"}
    )
    assert bad.status_code == 416


def test_zip_of_a_folder_streams_everything_visible(client: TestClient, env: Env) -> None:
    response = client.get("/ui/files/zip", params={"path": "lib:"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert "lib/docs/report.txt" in names
    assert "lib/docs/sub/deep.txt" in names
    assert not any("private" in n or ".steward" in n or "link.txt" in n for n in names)


def test_zip_of_a_selection(client: TestClient) -> None:
    response = client.get(
        "/ui/files/zip", params=[("path", "lib:docs/report.txt"), ("path", "lib:docs/sub")]
    )
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(archive.namelist()) == ["report.txt", "sub/", "sub/deep.txt"]
    assert archive.read("report.txt") == b"quarterly report\n"


# --- changes ------------------------------------------------------------------------------


def test_new_folder_then_undo(client: TestClient, env: Env) -> None:
    made = client.post("/ui/files/mkdir", data={"folder": "lib:docs", "name": "Taxes"})
    assert made.status_code == 303
    assert (env.root / "docs/Taxes").is_dir()
    page = client.get(made.headers["location"])
    # The message and its Undo button are one bar (the layout's own notice isn't used).
    bar = re.search(r'<div class="notice notice-bar"[^>]*>(.*?)</div>', page.text, re.S)
    assert bar, page.text
    assert "Folder created." in bar.group(1)
    assert ">Undo</button>" in bar.group(1)
    assert page.text.count("Folder created.") == 1
    undone = client.post("/ui/files/undo", data={"entries": _undo_ids(made), "back": "lib:docs"})
    assert undone.status_code == 303
    assert not (env.root / "docs/Taxes").exists()


def test_rename_and_a_collision(client: TestClient, env: Env) -> None:
    clash = client.post(
        "/ui/files/rename", data={"path": "lib:docs/notes.md", "new_name": "report.txt"}
    )
    assert clash.status_code == 409
    assert (env.root / "docs/notes.md").exists()
    ok = client.post("/ui/files/rename", data={"path": "lib:docs/notes.md", "new_name": "n.md"})
    assert ok.status_code == 303
    assert (env.root / "docs/n.md").read_text() == "# notes\n"


def test_move_several_and_undo_them_together(client: TestClient, env: Env) -> None:
    picker = client.get(
        "/ui/files/move",
        params=[
            ("path", "lib:docs/report.txt"),
            ("path", "lib:docs/notes.md"),
            ("to", "lib:photos"),
        ],
    )
    assert picker.status_code == 200
    assert "Move here" in picker.text
    moved = client.post(
        "/ui/files/move",
        data={"path": ["lib:docs/report.txt", "lib:docs/notes.md"], "to": "lib:photos"},
    )
    assert moved.status_code == 303
    assert (env.root / "photos/report.txt").exists()
    assert (env.root / "photos/notes.md").exists()
    client.post("/ui/files/undo", data={"entries": _undo_ids(moved), "back": "lib:docs"})
    assert (env.root / "docs/report.txt").exists()
    assert (env.root / "docs/notes.md").exists()


def test_a_folder_cant_be_moved_into_itself(client: TestClient) -> None:
    picker = client.get("/ui/files/move", params=[("path", "lib:docs"), ("to", "lib:docs/sub")])
    assert "can't be moved into itself" in picker.text
    assert "Move here" not in picker.text


def test_moving_into_a_locked_folder_is_refused_from_normal_views(
    client: TestClient, env: Env
) -> None:
    response = client.post(
        "/ui/files/move", data={"path": ["lib:docs/report.txt"], "to": "lib:private"}
    )
    assert response.status_code == 404
    assert (env.root / "docs/report.txt").exists()


def test_delete_needs_the_password_and_goes_to_the_trash(client: TestClient, env: Env) -> None:
    from api_support import PASSWORD

    confirm = client.get("/ui/files/delete", params={"path": "lib:docs/report.txt"})
    assert confirm.status_code == 200
    # The login counted as a password check (window 15 min), so this goes straight through.
    deleted = client.post("/ui/files/delete", data={"path": ["lib:docs/report.txt"]})
    assert deleted.status_code == 303, deleted.text
    assert not (env.root / "docs/report.txt").exists()
    trashed = list((env.root / ".steward/trash").rglob("report.txt"))
    assert len(trashed) == 1

    # With re-auth "every time", the next delete asks for the password first.
    env.runtime.auth.set_settings_from_host({"reauth_minutes": 0}, actor="test")
    with env.runtime.dbs.state.write() as conn:
        from sqlalchemy import update

        from steward.core.db.schema import sessions

        conn.execute(update(sessions).values(reauth_at=None))
    asked = client.post(
        "/ui/files/delete",
        data={"path": ["lib:docs/notes.md"]},
        headers={"Referer": "http://testserver/ui/files/delete?path=lib:docs/notes.md"},
    )
    assert asked.status_code == 303
    assert asked.headers["location"].startswith("/reauth?next=")
    assert (env.root / "docs/notes.md").exists()
    page = client.get("/reauth")
    from api_support import form_token

    client.post("/reauth", data={"password": PASSWORD, "csrf_token": form_token(page.text)})
    done = client.post("/ui/files/delete", data={"path": ["lib:docs/notes.md"]})
    assert done.status_code == 303
    assert not (env.root / "docs/notes.md").exists()


def test_changes_are_journaled_as_the_owner(client: TestClient, env: Env) -> None:
    client.post("/ui/files/mkdir", data={"folder": "lib:", "name": "made"})
    entries = env.runtime.executor.entries(limit=5)
    assert entries[0].action == "file.mkdir"
    assert entries[0].actor.startswith("user:")


def test_search_results_link_into_the_file_manager(client: TestClient) -> None:
    page = client.get("/ui/search", params={"q": "report"})
    assert "/ui/files/view?path=lib:docs/report.txt" in page.text
    assert '/ui/files?path=lib:docs"' in page.text


def test_the_module_can_be_turned_off(tmp_path: Path) -> None:
    from support import eps, write_config

    from steward.core.config import load_config
    from steward.core.runtime import build_runtime

    config_dir = write_config(
        tmp_path / "config",
        steward={"data_dir": str(tmp_path / "data")},
        modules={"modules": {"files_ui": {"enabled": False}}},
    )
    runtime = build_runtime(load_config(config_dir), entry_points=eps(FilesUiModule))
    runtime.upgrade_databases()
    try:
        with TestClient(create_app(runtime), follow_redirects=False) as c:
            assert c.get("/ui/files").status_code in (303, 404)
            assert c.get("/static/m/files_ui/files.css").status_code == 404
    finally:
        runtime.close()


def test_writes_only_happen_through_the_executor(env: Env) -> None:
    # The module imports nothing from the core (import-linter checks it too).
    import steward.modules.files_ui.routes as routes

    source = Path(routes.__file__).read_text()
    assert "steward.core" not in source
    assert "os." not in source
    assert os.path.exists(routes.__file__)
