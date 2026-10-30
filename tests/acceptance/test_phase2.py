# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Phase 2 acceptance (plan §19): one test per criterion, end to end over HTTP.

Each test starts from a fresh install finished through the setup wizard (no model, no
proxy, the built-in `files_ui` and `shares` modules on) and runs on two folder layouts
with different names and mappings: in one the locked folder is chosen in the wizard,
in the other it's locked from Files afterwards. The detailed behaviour behind each
criterion is tested in `tests/integration/test_uploads.py`, `test_locked_folder.py`,
`test_shares.py` and `tests/privacy/test_locked_routes.py`; the 5 GB size is checked
by hand in a real browser (the Phase 2 test environment).

Discovery runs in the worker, which these tests don't start: `Install.scan()` stands
in for it after the wizard and after changes on disk.
"""

from __future__ import annotations

import asyncio
import io
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from api_support import PASSWORD, SETUP_TOKEN, USERNAME, csrf_of, form_token
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from support import eps, use_fast_hashes, write_config
from typer.testing import CliRunner

from steward.api import create_app
from steward.cli import app as cli
from steward.core.config import DiscoverySettings, load_config
from steward.core.db.schema import files, sessions
from steward.core.discovery import Scanner
from steward.core.folders import FolderRef, load_policy
from steward.core.runtime import Runtime, build_runtime
from steward.core.setup import SetupToken
from steward.modules.files_ui import FilesUiModule
from steward.modules.photos import PhotosModule
from steward.modules.shares import SharesModule

MODULES = (FilesUiModule, PhotosModule, SharesModule)
CHUNK = 1024 * 1024  # uploads.chunk_mib: 1
SECRET_NAME = "okapi"
"""In the name of every file inside the locked folder; never in a normal response."""
SECRET_QUERY = SECRET_NAME[1:]
"""Finds those names; requests use it instead of `SECRET_NAME`, because pages echo the
query (which would look like a leak)."""
SECRET_CONTENT = "ZEBRA-7f3a-locked"
"""In the content of every file inside the locked folder."""
NO_EXPIRY_WARNING = "A link with no expiry keeps working until you revoke it"


@dataclass(frozen=True)
class Layout:
    name: str
    library: str
    tree: dict[str, str]
    types: dict[str, str]
    """Top-level folder → type, as chosen on the wizard's types step."""
    locked: str
    """The locked folder (relative path)."""
    locked_in_wizard: bool
    """Chosen as the private folder in the wizard; otherwise locked from Files later."""
    shared_file: str
    shared_folder: str
    upload_folder: str


LAYOUTS = (
    Layout(
        name="wizard-private",
        library="main",
        tree={
            "Documents/report.txt": "quarterly report\n",
            "Documents/Notes/todo.md": "- call the plumber\n",
            "Documents/Notes/ideas.txt": "a garden\n",
            "Photos/2025/beach.png": "\x89PNG\r\n\x1a\nbeach",
            f"Private/{SECRET_NAME}-diary.txt": f"{SECRET_CONTENT} diary\n",
            f"Private/Taxes/{SECRET_NAME}-2025.pdf": f"%PDF-1.4 {SECRET_CONTENT}\n",
        },
        types={"Documents": "documents", "Photos": "media", "Private": ""},
        locked="Private",
        locked_in_wizard=True,
        shared_file="Documents/report.txt",
        shared_folder="Documents/Notes",
        upload_folder="Documents",
    ),
    Layout(
        name="locked-from-files",
        library="home",
        tree={
            "work/notes.md": "# notes\n",
            "work/specs/plan.txt": "the plan\n",
            "work/specs/budget.txt": "the budget\n",
            "media/pic.png": "\x89PNG\r\n\x1a\npic",
            f"personal/vault/{SECRET_NAME}-journal.txt": f"{SECRET_CONTENT} journal\n",
            f"personal/vault/deep/{SECRET_NAME}-letter.txt": f"{SECRET_CONTENT} letter\n",
            "personal/recipes.txt": "bread\n",
        },
        types={"work": "documents", "media": "media", "personal": ""},
        locked="personal/vault",
        locked_in_wizard=False,
        shared_file="work/notes.md",
        shared_folder="work/specs",
        upload_folder="work",
    ),
)


@dataclass
class Install:
    """A fresh install, finished through the setup wizard, with the owner logged in."""

    tmp_path: Path
    layout: Layout
    runtime: Runtime = field(init=False)
    owner: TestClient = field(init=False)

    def __post_init__(self) -> None:
        root = self.root
        for relpath, content in self.layout.tree.items():
            path = root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("latin-1"))
        write_config(
            self.tmp_path / "config",
            steward={
                "data_dir": str(self.tmp_path / "data"),
                "setup": {"library_dirs": [str(self.tmp_path / "libraries")]},
                "uploads": {"chunk_mib": 1},
            },
            modules={"modules": {m.name: {"enabled": True} for m in MODULES}},
        )
        self.start()
        self._run_wizard()
        self.scan()
        if not self.layout.locked_in_wizard:
            locked = self.owner.post("/ui/files/lock", data={"path": self.ref(self.layout.locked)})
            assert locked.status_code == 303, locked.text

    @property
    def root(self) -> Path:
        return self.tmp_path / "libraries" / self.layout.library

    def ref(self, relpath: str = "") -> str:
        return f"{self.layout.library}:{relpath}"

    # --- the server -----------------------------------------------------------------------

    def start(self, cookies: Any = None) -> None:
        """Start the server: what `steward api` does (migrations, then the app)."""
        config = load_config(self.tmp_path / "config")
        self.runtime = build_runtime(config, entry_points=eps(*MODULES))
        self.runtime.upgrade_databases()
        app = create_app(self.runtime, setup_token=SetupToken(SETUP_TOKEN))
        self.owner = TestClient(app, follow_redirects=False)
        self.owner.__enter__()
        if cookies is not None:
            self.owner.cookies.update(cookies)
            self.owner.headers["X-CSRF-Token"] = csrf_of(self.owner)

    def stop(self) -> Any:
        """Stop the server; returns the browser's cookies (the session outlives it)."""
        cookies = dict(self.owner.cookies.items())
        self.owner.__exit__(None, None, None)
        self.runtime.close()
        return cookies

    def restart(self) -> None:
        self.start(self.stop())

    def visitor(self) -> TestClient:
        """Someone else's browser: no session."""
        return TestClient(self.owner.app, follow_redirects=False)

    def token_client(self) -> TestClient:
        user = self.runtime.auth.get_user(username=USERNAME)
        assert user is not None
        _, secret = self.runtime.auth.issue_token(
            user.user_id, "acceptance", ["read", "propose"], actor="test"
        )
        client = self.visitor()
        client.headers["Authorization"] = f"Bearer {secret}"
        return client

    def scan(self) -> None:
        Scanner(self.runtime.dbs, DiscoverySettings()).scan(self.layout.library)

    # --- the owner --------------------------------------------------------------------------

    def _run_wizard(self) -> None:
        c = self.owner
        page = c.get("/setup")
        csrf = form_token(page.text)
        assert c.post("/setup", data={"token": SETUP_TOKEN, "csrf_token": csrf}).status_code == 303
        owner = {"username": USERNAME, "password": PASSWORD, "confirm": PASSWORD}
        assert c.post("/setup/owner", data={**owner, "csrf_token": csrf}).status_code == 303
        csrf = form_token(c.get("/setup/libraries").text)
        added = c.post(
            "/setup/libraries",
            data={
                "action": "add",
                "library_id": self.layout.library,
                "path": str(self.root),
                "csrf_token": csrf,
            },
        )
        assert added.status_code == 200, added.text
        c.post("/setup/libraries", data={"action": "next", "csrf_token": csrf})
        types = {f"type:{self.ref(f)}": t for f, t in self.layout.types.items()}
        assert c.post("/setup/types", data={**types, "csrf_token": csrf}).status_code == 303
        if self.layout.locked_in_wizard:
            private = {"action": "existing", "folder": self.ref(self.layout.locked)}
        else:
            # Another private folder made by the wizard; the tests use the one locked later.
            private = {"action": "create", "library_id": self.layout.library, "new_name": "Safe"}
        assert c.post("/setup/private", data={**private, "csrf_token": csrf}).status_code == 303
        assert c.post("/setup/roles", data={"csrf_token": csrf}).status_code == 303
        assert c.get("/setup/done").status_code == 200
        assert c.get("/ui/").status_code == 200
        c.headers["X-CSRF-Token"] = csrf_of(c)

    def forget_reauth(self) -> None:
        """Make the session's last password check old: the login no longer counts."""
        with self.runtime.dbs.state.write() as conn:
            conn.execute(update(sessions).values(reauth_at=None))

    def reauth(self) -> None:
        page = self.owner.get("/reauth")
        done = self.owner.post(
            "/reauth", data={"password": PASSWORD, "csrf_token": form_token(page.text)}
        )
        assert done.status_code == 303, done.text

    def is_locked(self, relpath: str) -> bool:
        with self.runtime.dbs.state.read() as conn:
            settings = load_policy(conn).settings_for(FolderRef(self.layout.library, relpath))
        return settings.locked

    def file_ids(self, prefix: str) -> list[str]:
        with self.runtime.dbs.state.read() as conn:
            rows = conn.execute(
                select(files.c.file_id).where(files.c.relpath.startswith(prefix + "/"))
            )
            return [str(r.file_id) for r in rows]


@pytest.fixture(autouse=True)
def fast_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fast_hashes(monkeypatch)


@pytest.fixture(params=LAYOUTS, ids=lambda layout: layout.name)
def install(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Install]:
    i = Install(tmp_path, request.param)
    yield i
    i.stop()


# --- 1. a big upload through the browser, resumed after a dropped connection ------------


def _put(client: TestClient, upload_id: str, offset: int, data: bytes) -> Any:
    return client.put(
        f"/api/v1/uploads/{upload_id}",
        content=data,
        headers={"Content-Type": "application/offset+octet-stream", "Upload-Offset": str(offset)},
    )


def _put_and_drop(client: TestClient, upload_id: str, offset: int, part: bytes) -> int:
    """Send the first bytes of a chunk, then drop the connection (what a browser going
    offline mid-request does). Talks ASGI directly: a test client can't drop a request."""
    headers = [
        (b"host", b"testserver"),
        (b"content-type", b"application/offset+octet-stream"),
        (b"upload-offset", str(offset).encode()),
        (b"content-length", str(CHUNK).encode()),
        (b"x-csrf-token", client.headers["X-CSRF-Token"].encode()),
        (b"cookie", "; ".join(f"{k}={v}" for k, v in client.cookies.items()).encode()),
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "http",
        "path": f"/api/v1/uploads/{upload_id}",
        "raw_path": f"/api/v1/uploads/{upload_id}".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    messages: list[dict[str, Any]] = [
        {"type": "http.request", "body": part, "more_body": True},
        {"type": "http.disconnect"},
    ]
    status: list[int] = []

    async def receive() -> dict[str, Any]:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status.append(message["status"])

    asyncio.run(client.app(scope, receive, send))  # type: ignore[arg-type]
    return status[0]


def test_an_upload_resumes_after_a_dropped_connection_and_a_restart(install: Install) -> None:
    data = bytes(i % 251 for i in range(CHUNK * 5 + 123))
    folder = install.ref(install.layout.upload_folder)
    page = install.owner.get("/ui/files", params={"path": folder})
    assert 'id="uploads"' in page.text  # the browser client is on the folder page
    assert "/static/m/files_ui/upload.js" in page.text
    started = install.owner.post(
        "/api/v1/uploads", json={"folder": folder, "name": "film.mp4", "size": len(data)}
    )
    assert started.status_code == 201, started.text
    upload_id = started.json()["upload_id"]
    offset = 0
    for _ in range(2):
        offset = _put(install.owner, upload_id, offset, data[offset : offset + CHUNK]).json()[
            "offset"
        ]
    assert offset == 2 * CHUNK

    # The connection drops halfway through the third chunk: none of it is kept.
    assert (
        _put_and_drop(install.owner, upload_id, offset, data[offset : offset + CHUNK // 2]) == 400
    )
    assert install.owner.get(f"/api/v1/uploads/{upload_id}").json()["offset"] == offset

    # The server restarts; the browser keeps its session and asks where to carry on.
    install.restart()
    resumed = install.owner.get(f"/api/v1/uploads/{upload_id}")
    assert resumed.status_code == 200, resumed.text
    offset = resumed.json()["offset"]
    assert offset == 2 * CHUNK
    listed = install.owner.get("/api/v1/uploads").json()["uploads"]
    assert [u["upload_id"] for u in listed] == [upload_id]
    while offset < len(data):
        offset = _put(install.owner, upload_id, offset, data[offset : offset + CHUNK]).json()[
            "offset"
        ]
    done = install.owner.post(f"/api/v1/uploads/{upload_id}/finish", json={})
    assert done.status_code == 201, done.text
    path = f"{folder}/film.mp4"
    assert done.json()["path"] == path
    assert (install.root / install.layout.upload_folder / "film.mp4").read_bytes() == data
    temps = [p.name for p in (install.root / install.layout.upload_folder).iterdir()]
    assert not [n for n in temps if n.startswith(".steward-tmp")]
    assert install.owner.get("/api/v1/uploads").json()["uploads"] == []
    download = install.owner.get("/ui/files/raw", params={"path": path})
    assert download.content == data
    journal = install.owner.get("/ui/journal").text
    assert "film.mp4" in journal


# --- 2. a locked folder is hidden without a re-auth; uploading into it works --------------


def _normal_requests(install: Install) -> list[tuple[str, dict[str, Any]]]:
    """Every normal view, search and API read that could name or serve a locked file."""
    lib = install.ref()
    parent = install.ref(install.layout.locked.rpartition("/")[0])
    locked = install.ref(install.layout.locked)
    inside = sorted(p for p in install.layout.tree if p.startswith(install.layout.locked + "/"))
    requests: list[tuple[str, dict[str, Any]]] = [
        ("/ui/", {}),
        ("/ui/files", {"path": lib}),
        ("/ui/files", {"path": parent}),
        ("/ui/files", {"path": locked}),
        ("/ui/files/zip", {"path": lib}),
        ("/ui/files/zip", {"path": parent}),
        ("/ui/search", {"q": SECRET_QUERY}),
        ("/ui/search", {"q": "txt"}),
        ("/ui/journal", {}),
        ("/ui/shares/new", {"path": locked}),
        ("/api/v1/libraries", {}),
        ("/api/v1/folders", {"path": lib}),
        ("/api/v1/folders", {"path": parent}),
        ("/api/v1/folders", {"path": locked}),
        ("/api/v1/files", {"path": locked}),
        ("/api/v1/search", {"q": SECRET_QUERY}),
        ("/api/v1/search", {"q": "txt"}),
        ("/api/v1/journal", {}),
        ("/api/v1/uploads", {"scope": "locked"}),
    ]
    requests += [("/ui/files", {"kind": kind}) for kind in ("text", "document", "other")]
    for relpath in inside:
        requests += [
            ("/ui/files/view", {"path": install.ref(relpath)}),
            ("/ui/files/raw", {"path": install.ref(relpath)}),
        ]
    requests += [
        (f"/api/v1/files/{file_id}", {}) for file_id in install.file_ids(install.layout.locked)
    ]
    return requests


def _assert_nothing_locked(
    install: Install, client: TestClient, *, extra: tuple[str, ...] = ()
) -> None:
    for url, params in _normal_requests(install):
        response = client.get(url, params=params)
        body = response.content
        for marker in (SECRET_NAME, SECRET_CONTENT, *extra):
            assert marker.encode() not in body, (url, params, response.status_code)
        assert response.status_code < 500, (url, params, response.text)


def _assert_normal_views_work(install: Install) -> None:
    """The requests above really list, search and zip (a 404 would hide nothing)."""
    c = install.owner
    shared = install.layout.shared_file.rpartition("/")[2]
    whole = c.get("/ui/files/zip", params={"path": install.ref()})
    assert whole.status_code == 200
    with zipfile.ZipFile(io.BytesIO(whole.content)) as z:
        assert any(n.endswith("/" + shared) for n in z.namelist())
    assert shared in c.get("/ui/search", params={"q": shared}).text
    found = c.get("/api/v1/search", params={"q": shared}).json()
    assert shared in str(found)
    listing = c.get("/api/v1/folders", params={"path": install.ref()})
    assert listing.status_code == 200
    assert install.layout.upload_folder in listing.text


def test_a_locked_folder_is_hidden_without_a_reauth_and_takes_uploads(install: Install) -> None:
    assert install.is_locked(install.layout.locked)
    assert install.file_ids(install.layout.locked)  # discovery did see them

    # A session whose password check isn't recent, and an API token: nothing shows.
    install.forget_reauth()
    _assert_nothing_locked(install, install.owner)
    _assert_normal_views_work(install)
    gate = install.owner.get("/ui/locked")
    assert gate.status_code == 303
    assert gate.headers["location"].startswith("/reauth")
    token = install.token_client()
    _assert_nothing_locked(install, token)
    assert token.get("/ui/locked").status_code in (401, 403)
    assert token.get("/api/v1/search", params={"q": "x", "locked": "true"}).status_code == 403

    # After the password, the Locked Folder shows them and takes an upload.
    install.reauth()
    locked = install.ref(install.layout.locked)
    inside = install.owner.get("/ui/locked", params={"path": locked})
    assert inside.status_code == 200
    assert SECRET_NAME in inside.text
    content = f"{SECRET_CONTENT} uploaded\n".encode()
    name = f"{SECRET_NAME}-upload.txt"
    started = install.owner.post(
        "/api/v1/uploads",
        json={"folder": locked, "name": name, "size": len(content), "scope": "locked"},
    )
    assert started.status_code == 201, started.text
    upload_id = started.json()["upload_id"]
    assert _put(install.owner, upload_id, 0, content).status_code == 200
    assert install.owner.post(f"/api/v1/uploads/{upload_id}/finish").status_code == 201
    on_disk = install.root / install.layout.locked / name
    assert on_disk.read_bytes() == content
    install.scan()
    assert name in install.owner.get("/ui/locked", params={"path": locked}).text
    found = install.owner.get("/ui/locked/search", params={"q": "upload"})
    assert name in found.text

    # ...and the new file shows nowhere else either.
    install.forget_reauth()
    _assert_nothing_locked(install, install.owner, extra=("uploaded",))
    _assert_nothing_locked(install, token, extra=("uploaded",))


# --- 3. a permanent lock can't be removed from the UI or API ------------------------------


def test_a_permanent_lock_cant_be_removed_from_the_ui_or_api(install: Install) -> None:
    c = install.owner
    locked = install.ref(install.layout.locked)
    install.runtime.auth.set_settings_from_host({"reauth_minutes": 0}, actor="test")
    install.forget_reauth()
    asked = c.post("/ui/locked/lock", data={"path": locked, "permanent": "true"})
    assert asked.status_code == 303
    assert asked.headers["location"].startswith("/reauth")
    install.reauth()
    made = c.post("/ui/locked/lock", data={"path": locked, "permanent": "true"})
    assert made.status_code == 303, made.text
    install.reauth()  # window 0: making it permanent used up the last check
    page = c.get("/ui/locked", params={"path": locked})
    assert "locked permanently" in page.text
    entry_id = install.runtime.executor.entries(limit=1)[0].entry_id

    # Every way a user or a script could try, each after a fresh password check (so
    # nothing is refused only for want of a password).
    attempts: list[tuple[str, dict[str, Any]]] = [
        ("/ui/locked/unlock", {"data": {"path": locked}}),
        ("/api/v1/actions/folder.unlock", {"json": {"path": locked}}),
        ("/api/v1/actions/folder.lock", {"json": {"path": locked, "permanent": False}}),
        (f"/api/v1/journal/{entry_id}/undo", {}),
        (f"/ui/journal/{entry_id}/undo", {}),
        ("/ui/locked/undo", {"data": {"entries": str(entry_id)}}),
        ("/ui/files/undo", {"data": {"entries": str(entry_id)}}),
        ("/ui/settings/folders/lock", {"data": {"folder": locked, "locked": "false"}}),
        ("/ui/settings/folders/map", {"data": {"folder": locked, "type_name": "documents"}}),
        ("/ui/settings/folders/unmap", {"data": {"folder": locked}}),
    ]
    for url, kwargs in attempts:
        install.reauth()
        response = c.post(url, **kwargs)
        assert response.status_code != 500, (url, response.text)
        assert install.is_locked(install.layout.locked), url
    preview = c.get("/ui/locked/unlock", params={"path": locked})
    assert "permanently locked" in preview.text
    assert 'action="/ui/locked/unlock"' not in preview.text
    install.forget_reauth()
    assert c.get("/ui/files", params={"path": locked}).status_code == 404
    install.reauth()
    assert "locked permanently" in c.get("/ui/locked", params={"path": locked}).text

    # Only the host can lift it.
    result = CliRunner().invoke(
        cli,
        [
            "--config-dir",
            str(install.tmp_path / "config"),
            "folders",
            "unlock",
            locked,
            "--permanent",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not install.is_locked(install.layout.locked)
    assert c.get("/ui/files", params={"path": locked}).status_code == 200


# --- 4. a no-expiry share warns and needs a re-auth; a revoked one ends ------------------


def _created(page: str) -> tuple[str, str]:
    """(share_id, token) from the "link created" page."""
    token = re.search(r"/s/([A-Za-z0-9_-]{22})", page)
    share_id = re.search(r"/ui/shares/([0-9a-f]{32})", page)
    assert token, page
    assert share_id, page
    return share_id.group(1), token.group(1)


def test_a_no_expiry_share_warns_needs_a_reauth_and_is_listed(install: Install) -> None:
    c = install.owner
    path = install.ref(install.layout.shared_file)
    dialog = c.get("/ui/shares/new", params={"path": path})
    assert dialog.status_code == 200
    assert NO_EXPIRY_WARNING in dialog.text
    install.forget_reauth()
    data = {"path": path, "expiry": "never"}
    asked = c.post("/ui/shares/new", data=data)
    assert asked.status_code == 303
    assert asked.headers["location"].startswith("/reauth")
    assert "no expiry" not in c.get("/ui/shares").text  # nothing was made
    install.reauth()
    created = c.post("/ui/shares/new", data=data)
    assert created.status_code == 200, created.text
    assert "never expires" in created.text
    share_id, token = _created(created.text)
    listed = c.get("/ui/shares")
    assert share_id in listed.text
    assert "no expiry" in listed.text
    assert re.search(r'Shares <span class="badge"[^>]*>1</span>', listed.text)
    assert install.visitor().get(f"/s/{token}").status_code == 200

    # Scripts get the same rule through the API.
    install.forget_reauth()
    folder = install.ref(install.layout.shared_folder)
    params = {"path": folder, "expires_at": None, "password": None}
    refused = c.post("/api/v1/actions/shares.create", json=params)
    assert refused.status_code == 403
    assert "password" in refused.json()["error"]
    install.reauth()
    made = c.post("/api/v1/actions/shares.create", json=params)
    assert made.status_code == 201, made.text
    assert re.search(r'Shares <span class="badge"[^>]*>2</span>', c.get("/ui/shares").text)


def test_a_revoked_share_moves_to_past_and_its_link_stops(install: Install) -> None:
    c = install.owner
    folder = install.ref(install.layout.shared_folder)
    created = c.post("/ui/shares/new", data={"path": folder, "expiry": "7"})
    assert created.status_code == 200, created.text
    share_id, token = _created(created.text)
    visitor = install.visitor()
    names = sorted(
        p.rpartition("/")[2]
        for p in install.layout.tree
        if p.startswith(install.layout.shared_folder + "/")
    )
    urls = [f"/s/{token}", f"/s/{token}?zip=1"]
    urls += [f"/s/{token}/{quote(n)}" for n in names]
    urls += [f"/s/{token}/{quote(n)}?raw=1" for n in names]
    listing = visitor.get(urls[0])
    assert listing.status_code == 200
    assert all(n in listing.text for n in names)
    with zipfile.ZipFile(io.BytesIO(visitor.get(urls[1]).content)) as z:
        top = install.layout.shared_folder.rpartition("/")[2]
        assert sorted(z.namelist()) == [f"{top}/", *(f"{top}/{n}" for n in names)]
    for url in urls[2:]:
        assert visitor.get(url).status_code == 200, url

    revoked = c.post(f"/ui/shares/{share_id}/revoke")
    assert revoked.status_code == 303
    active = c.get("/ui/shares")
    assert share_id not in active.text
    past = c.get("/ui/shares", params={"status": "past"})
    assert share_id in past.text
    assert "revoked" in past.text
    unknown = visitor.get(f"/s/{'A' * 22}")
    assert unknown.status_code == 404
    for url in urls:
        gone = visitor.get(url)
        assert gone.status_code == 404, url
        assert gone.text == unknown.text, url  # nothing tells it ever existed
    # Revoking is final.
    assert install.runtime.executor.entries(limit=1)[0].undoable is False
