# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`documents` end to end: discovery -> extract -> chunk -> full-text search, on two
layouts, with the scope and privacy rules (locked, excluded, metadata-only, media)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from api_support import USERNAME, log_in, make_owner
from doc_fixtures import make_docx, make_encrypted_pdf, make_odt, make_pdf
from fastapi.testclient import TestClient
from sqlalchemy import select
from support import make_runtime, use_fast_hashes

from steward.api import create_app
from steward.core.config import DiscoverySettings
from steward.core.db.schema import chunks, files, stage_runs, texts
from steward.core.discovery import Scanner
from steward.core.folders import FolderRef, load_policy, store
from steward.core.runtime import Runtime
from steward.core.search import search_all, search_text
from steward.core.worker import Worker
from steward.modules.documents import DocumentsModule

ACTOR = "user:1"
SECRET_WORD = "zebracorn"
EXCLUDED_WORD = "kdbxsecretword"

LAYOUTS: dict[str, dict[str, Any]] = {
    "numbered": {
        "docs": "10-documents",
        "private": "90-private",
        "archive": "80-archive",
        "media": "20-media",
        "map": {
            "10-documents": "documents",
            "90-private": "private",
            "80-archive": "archive",
            "20-media": "media",
        },
    },
    "flat": {
        "docs": "Documents",
        "private": "Private",
        "archive": "Old",
        "media": "Photos",
        "map": {
            "Documents": "documents",
            "Private": "private",
            "Old": "archive",
            "Photos": "media",
        },
    },
}


class Env:
    def __init__(self, tmp_path: Path, layout: str) -> None:
        self.spec = LAYOUTS[layout]
        self.runtime: Runtime = make_runtime(tmp_path, (DocumentsModule,))
        self.root = tmp_path / "lib"
        d, p = self.spec["docs"], self.spec["private"]
        self.files: dict[str, bytes] = {
            f"{d}/report.pdf": make_pdf(["Cover page", "The quarterly revenue grew"]),
            f"{d}/letter.docx": make_docx([("Heading1", "Lease"), (None, "The landlord agrees")]),
            f"{d}/minutes.odt": make_odt(
                [(True, "Meeting"), (False, "Budget approved unanimously")]
            ),
            f"{d}/notes/plan.md": b"# Garden plan\nPlant tomatoes in spring\n",
            f"{d}/page.html": b"<h1>Recipe</h1><p>Mix flour and water</p><script>evil()</script>",
            f"{d}/script.py": b"def compute_mortgage():\n    return 1\n",
            f"{d}/locked-out.pdf": make_encrypted_pdf(["hidden encrypted words"]),
            f"{d}/broken.pdf": b"%PDF-1.4 garbage that is not a pdf",
            f"{d}/vault.kdbx": f"{EXCLUDED_WORD} plain text".encode(),
            f"{d}/.env": f"TOKEN={EXCLUDED_WORD}".encode(),
            f"{d}/copy-of-diary.txt": b"public copy of the diary mentions tomatoes",
            f"{p}/diary.txt": f"my {SECRET_WORD} diary entry".encode(),
            f"{self.spec['archive']}/old.txt": b"archived ledger quarterly",
            f"{self.spec['media']}/caption.txt": b"beach sunset quarterly",
        }
        for relpath, data in self.files.items():
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        with self.runtime.dbs.state.write() as conn:
            store.upsert_library(conn, "lib", root=str(self.root), actor=ACTOR)
            for relpath, type_name in self.spec["map"].items():
                store.set_type(conn, FolderRef("lib", relpath), type_name, actor=ACTOR)
            store.ensure_default_roles(conn, actor=ACTOR)
        self.scan()

    def scan(self) -> None:
        Scanner(self.runtime.dbs, DiscoverySettings()).scan("lib")
        Worker(self.runtime).drain()

    def hash_of(self, relpath: str) -> str:
        with self.runtime.dbs.state.read() as conn:
            value = conn.execute(
                select(files.c.content_hash).where(files.c.relpath == relpath)
            ).scalar_one()
        assert value is not None
        return str(value)

    def runs(self, relpath: str) -> dict[str, tuple[str, str | None]]:
        with self.runtime.dbs.index.read() as conn:
            rows = conn.execute(
                select(stage_runs.c.stage, stage_runs.c.status, stage_runs.c.error).where(
                    stage_runs.c.content_hash == self.hash_of(relpath)
                )
            ).all()
        return {r.stage: (r.status, r.error) for r in rows}

    def find(self, query: str, *, scope: str = "normal", mode: str = "text") -> list[str]:
        with self.runtime.dbs.state.read() as conn:
            policy = load_policy(conn)
        results = search_all(self.runtime.dbs, policy, query, scope=scope, mode=mode)  # type: ignore[arg-type]
        return [h.relpath for h in results.hits]


@pytest.fixture(params=sorted(LAYOUTS))
def env(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path, request.param)
    yield e
    e.runtime.close()


def test_documents_are_extracted_chunked_and_searchable(env: Env) -> None:
    d = env.spec["docs"]
    for name in (
        "report.pdf",
        "letter.docx",
        "minutes.odt",
        "notes/plan.md",
        "page.html",
        "script.py",
    ):
        runs = env.runs(f"{d}/{name}")
        assert runs["documents.extract"][0] == "done", (name, runs)
        assert runs["documents.chunk"][0] == "done", (name, runs)
    assert env.find("quarterly revenue") == [f"{d}/report.pdf"]
    assert env.find("landlord") == [f"{d}/letter.docx"]
    assert env.find("unanimously") == [f"{d}/minutes.odt"]
    assert env.find("TOMATOES") == sorted([f"{d}/notes/plan.md", f"{d}/copy-of-diary.txt"])
    assert env.find("flour") == [f"{d}/page.html"]
    assert env.find("evil") == []  # scripts aren't text
    assert env.find("compute_mortgage") == [f"{d}/script.py"]


def test_hits_carry_page_and_snippet(env: Env) -> None:
    with env.runtime.dbs.state.read() as conn:
        policy = load_policy(conn)
    hit = search_text(env.runtime.dbs, policy, "revenue").hits[0]
    assert hit.page == 2
    assert ("revenue", True) in hit.snippet
    assert "".join(t for t, _ in hit.snippet).startswith("The quarterly revenue")


def test_locked_text_is_only_found_in_the_locked_scope(env: Env) -> None:
    assert env.find(SECRET_WORD) == []
    assert env.find(SECRET_WORD, mode="all") == []
    assert env.find(SECRET_WORD, scope="locked") == [f"{env.spec['private']}/diary.txt"]
    # The public copy shares no content with the locked one, but words both have are
    # found only through the visible file.
    assert env.find("diary") == [f"{env.spec['docs']}/copy-of-diary.txt"]


def test_excluded_files_are_never_read(env: Env) -> None:
    d = env.spec["docs"]
    with env.runtime.dbs.state.read() as conn:
        rows = conn.execute(
            select(files.c.relpath, files.c.content_hash).where(
                files.c.relpath.in_([f"{d}/vault.kdbx", f"{d}/.env"])
            )
        ).all()
    assert len(rows) == 2
    assert all(r.content_hash is None for r in rows)
    with env.runtime.dbs.index.read() as conn:
        bodies = [
            *conn.execute(select(texts.c.body)).scalars(),
            *conn.execute(select(chunks.c.body)).scalars(),
        ]
    assert not any(EXCLUDED_WORD in b for b in bodies)
    assert env.find(EXCLUDED_WORD, mode="all") == []


def test_folder_types_decide_where_text_is_read(env: Env) -> None:
    assert "documents.extract" not in env.runs(f"{env.spec['archive']}/old.txt")
    assert "documents.extract" not in env.runs(f"{env.spec['media']}/caption.txt")
    assert env.find("ledger") == []
    assert env.find("sunset") == []


def test_unreadable_documents_are_skipped_with_a_reason(env: Env) -> None:
    d = env.spec["docs"]
    assert env.runs(f"{d}/locked-out.pdf")["documents.extract"] == ("skipped", "encrypted")
    status, reason = env.runs(f"{d}/broken.pdf")["documents.extract"]
    assert status == "skipped"
    assert reason is not None
    assert reason.startswith("unreadable (")
    assert env.runs(f"{d}/broken.pdf")["documents.chunk"] == ("skipped", "no text")


def test_an_edited_file_is_reindexed(env: Env) -> None:
    path = env.root / env.spec["docs"] / "notes/plan.md"
    path.write_bytes(b"# Garden plan\nPlant potatoes instead\n")
    os.utime(path, ns=(10**18, 10**18))
    env.scan()
    assert env.find("potatoes") == [f"{env.spec['docs']}/notes/plan.md"]
    assert env.find("tomatoes") == [f"{env.spec['docs']}/copy-of-diary.txt"]


def test_names_and_text_are_merged(env: Env) -> None:
    d = env.spec["docs"]
    # "plan" is in one file's name and another's text? Only the name here; "garden"
    # is in the text of plan.md; "report" only in a name.
    assert env.find("report", mode="all") == [f"{d}/report.pdf"]
    assert env.find("report", mode="text") == []
    assert env.find("garden", mode="names") == []
    assert env.find("garden", mode="all") == [f"{d}/notes/plan.md"]


def test_rerunning_the_pipeline_changes_nothing(env: Env) -> None:
    with env.runtime.dbs.index.read() as conn:
        before = conn.execute(select(chunks.c.content_hash, chunks.c.seq, chunks.c.body)).all()
    env.scan()
    with env.runtime.dbs.index.read() as conn:
        after = conn.execute(select(chunks.c.content_hash, chunks.c.seq, chunks.c.body)).all()
    assert sorted(before) == sorted(after)


# --- over HTTP ---------------------------------------------------------------------


@pytest.fixture
def client(env: Env, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    use_fast_hashes(monkeypatch)
    make_owner(env.runtime)
    with TestClient(create_app(env.runtime), follow_redirects=False) as c:
        yield c


def test_search_page_shows_snippets(env: Env, client: TestClient) -> None:
    log_in(client)
    page = client.get("/ui/search", params={"q": "revenue"})
    assert page.status_code == 200
    assert "<mark>revenue</mark>" in page.text
    assert "p. 2" in page.text
    names_only = client.get("/ui/search", params={"q": "revenue", "in": "names"})
    assert "report.pdf" not in names_only.text
    hidden = client.get("/ui/search", params={"q": SECRET_WORD})
    assert "diary.txt" not in hidden.text


def test_api_search_in_text(env: Env, client: TestClient) -> None:
    user = env.runtime.auth.get_user(username=USERNAME)
    _, secret = env.runtime.auth.issue_token(user.user_id, "t", ["read"], actor="test")
    headers = {"Authorization": f"Bearer {secret}"}
    body = client.get(
        "/api/v1/search", params={"q": "landlord", "in": "text"}, headers=headers
    ).json()
    assert body["in"] == "text"
    assert [h["name"] for h in body["hits"]] == ["letter.docx"]
    assert "landlord" in body["hits"][0]["snippet"]
    locked = client.get("/api/v1/search", params={"q": SECRET_WORD}, headers=headers).json()
    assert locked["hits"] == []
    bad = client.get("/api/v1/search", params={"q": "x", "in": "everything"}, headers=headers)
    assert bad.status_code == 400


# --- CLI -------------------------------------------------------------------------------


def test_cli_files_text_and_search(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from steward.cli import app
    from steward.modules.files_ui import FilesUiModule
    from steward.modules.shares import SharesModule

    # The CLI loads every installed built-in module, so their migrations must exist.
    runtime = make_runtime(tmp_path, (DocumentsModule, FilesUiModule, SharesModule))
    root = tmp_path / "lib"
    (root / "docs").mkdir(parents=True)
    (root / "docs/report.pdf").write_bytes(make_pdf(["Cover", "Revenue rose sharply"]))
    with runtime.dbs.state.write() as conn:
        store.upsert_library(conn, "lib", root=str(root), actor=ACTOR)
        store.ensure_default_roles(conn, actor=ACTOR)
    Scanner(runtime.dbs, DiscoverySettings()).scan("lib")
    Worker(runtime).drain()
    runtime.close()

    def run(*args: str) -> Any:
        return CliRunner().invoke(app, ["--config-dir", str(tmp_path / "config"), *args])

    shown = run("files", "text", "lib:docs/report.pdf")
    assert shown.exit_code == 0, shown.output
    listed = run("stages", "list")
    assert "documents.chunk v1  [cpu]  mime [text, text:ocr] -> chunks" in listed.output
    assert "-- page 2" in shown.output
    assert "Revenue rose sharply" in shown.output
    found = run("search", "revenue", "--in", "text")
    assert found.exit_code == 0, found.output
    assert "lib:docs/report.pdf" in found.output
    assert "p. 2: [Revenue] rose sharply" in found.output
    assert run("search", "revenue", "--in", "names").output.startswith("(0 shown")
    assert run("search", "x", "--in", "bogus").exit_code != 0
    assert run("files", "text", "lib:docs/missing.pdf").exit_code != 0
