# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Phase 3 acceptance (plan §19): one test per criterion, end to end, on two folder
layouts with different names and mappings, with a fake model server and stand-ins for
`tesseract` and `exiftool`.

The model is fake, so ">90% get a tag" here shows that the pipeline tags what a model
chooses and loses nothing on the way; how well a real model chooses is `steward eval
apply_tags` (its `tagged` score). Details are tested in `tests/integration/`:
`test_reindex.py`, `test_evals.py`, `test_tags.py`, `test_ocr.py`, `test_sidecars.py`.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from doc_fixtures import make_pdf
from model_server import FakeModelServer, llm_config, model_server
from ocr_support import SCANNED_WORD, make_fake_tesseract
from photos_support import make_fake_exiftool, make_jpeg
from sqlalchemy import select, update
from support import eps, write_config
from typer.testing import CliRunner

from steward.cli import app as cli
from steward.core import queue, reindex, stages
from steward.core.config import load_config
from steward.core.db.schema import files, jobs, journal
from steward.core.discovery import Scanner, Watcher
from steward.core.folders import FolderRef, load_policy, store
from steward.core.runtime import Runtime, build_runtime
from steward.core.search import search_all
from steward.core.tags import effective
from steward.core.tags import io as tag_io
from steward.core.worker import Worker
from steward.modules.documents import DocumentsModule
from steward.modules.fields import FieldsModule
from steward.modules.files_ui import FilesUiModule
from steward.modules.photos import PhotosModule
from steward.modules.search import SearchModule
from steward.modules.tags import TagsModule

MODULES = (DocumentsModule, SearchModule, FieldsModule, TagsModule, PhotosModule, FilesUiModule)
ACTOR = "user:1"
SIDECARS = "photos.sidecars"
CANARY = "WOMBAT-canary-5d1e"
"""In the password database; never anywhere else."""
PICKS = {
    "invoice": "finance/invoice",
    "receipt": "finance/receipt",
    "tenant": "home",
    "beach": "photos/beach",
    "recipe": "recipes",
    "trip": "travel",
}
"""Word in what the model is given -> the tag it chooses."""
VOCABULARY = (
    "tags:\n"
    + "".join(f"  - name: {name}\n" for name in sorted({*PICKS.values(), "yours"}))
    + "  - name: unused\n"
)


def fake_model(body: dict[str, Any]) -> str:
    """Answers `fields`, `apply_tags` and `caption` from what it's sent."""
    system = str(body["messages"][1]["content"] if len(body["messages"]) > 1 else "")
    user = str(body["messages"][-1]["content"])
    if "image_url" in user:
        return "A sandy beach with waves."
    if "pull out a few facts" in system:
        doc_type = "invoice" if "invoice" in user.lower() else "note"
        return json.dumps({"doc_type": doc_type, "title": "A document"})
    if "fixed list" in system:
        about = user.lower()
        return json.dumps({"tags": sorted({tag for word, tag in PICKS.items() if word in about})})
    return "ok"


@dataclass(frozen=True)
class Layout:
    name: str
    inbox: str
    docs: str
    photos: str
    private: str
    library: str


LAYOUTS = (
    Layout("numbered", "00-inbox", "10-docs", "20-media", "90-private", "main"),
    Layout("flat", "Downloads", "Documents", "Photos", "Private", "home"),
)


@dataclass
class Install:
    tmp_path: Path
    layout: Layout
    server: FakeModelServer
    with_model: bool = True
    runtime: Runtime = field(init=False)

    def __post_init__(self) -> None:
        p = self.layout
        self.root = self.tmp_path / "libraries" / p.library
        self.tesseract = make_fake_tesseract(self.tmp_path / "bin")
        self.exiftool = make_fake_exiftool(self.tmp_path / "bin")
        self.tree: dict[str, bytes] = {
            f"{p.docs}/invoice-1.pdf": make_pdf(["Invoice 1 from a plumber. Total 120 USD."]),
            f"{p.docs}/invoice-2.pdf": make_pdf(["Invoice 2 from a roofer. Total 80 USD."]),
            f"{p.docs}/receipt.txt": b"Receipt for a coffee.\n",
            f"{p.docs}/lease.md": b"# Lease\nThe tenant pays on the first.\n",
            f"{p.docs}/recipes/bread.md": b"# Bread\nA recipe for a loaf.\n",
            f"{p.docs}/trip.txt": b"Notes about the trip to the coast.\n",
            f"{p.docs}/trip-copy.txt": b"Notes about the trip to the coast.\n",
            f"{p.docs}/passwords.kdbx": f"KDBX {CANARY}".encode(),
            f"{p.photos}/beach.jpg": make_jpeg(color=(200, 180, 120)),
            f"{p.photos}/forest.jpg": make_jpeg((640, 482), color=(30, 90, 40)),
            f"{p.private}/diary.txt": b"A receipt kept private.\n",
        }
        for relpath, data in self.tree.items():
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.write_config()
        self.start()
        with self.runtime.dbs.state.write() as conn:
            store.upsert_library(conn, p.library, root=str(self.root), actor=ACTOR)
            for folder, type_name in (
                (p.inbox, "inbox"),
                (p.docs, "documents"),
                (p.photos, "media"),
                (p.private, "private"),
            ):
                (self.root / folder).mkdir(exist_ok=True)
                store.set_type(conn, FolderRef(p.library, folder), type_name, actor=ACTOR)
            store.ensure_default_roles(conn, actor=ACTOR)
            tag_io.import_tags(conn, tag_io.parse(VOCABULARY), actor=ACTOR)
        self.server.default = fake_model
        self.settle()

    def write_config(self) -> None:
        llm = llm_config(self.server.url) if self.with_model else {}
        if self.with_model:
            chat = {"endpoint": "local", "model": "fake-chat"}
            llm["roles"].update(
                fields=chat,
                apply_tags=chat,
                caption={**chat, "capabilities": ["vision"]},
            )
        write_config(
            self.tmp_path / "config",
            steward={"data_dir": str(self.tmp_path / "data")},
            modules={
                "modules": {
                    **{m.name: {"enabled": True} for m in MODULES},
                    "shares": {"enabled": False},
                    "documents": {
                        "enabled": True,
                        "settings": {"ocr_command": str(self.tesseract)},
                    },
                    "photos": {
                        "enabled": True,
                        "settings": {"exif_command": str(self.exiftool), "caption_min_side": 16},
                    },
                    "tags": {"enabled": True, "settings": {"min_vocabulary": 2}},
                }
            },
            pipeline={
                "jobs": {"poll_seconds": 0.05},
                "discovery": {"debounce_seconds": 0.1},
                "schedules": {"core.reconcile": "off"},
            },
            llm=llm,
        )

    def start(self) -> None:
        config = load_config(self.tmp_path / "config")
        self.runtime = build_runtime(config, entry_points=eps(*MODULES))
        self.runtime.upgrade_databases()
        if stages.full_pass_needed(self.runtime.dbs, self.runtime.stages):
            with self.runtime.dbs.state.write() as conn:
                stages.enqueue_full_pass(conn)

    def stop(self) -> None:
        self.runtime.close()

    def settle(self) -> None:
        """A scan and everything it leads to, and a sync of the sidecars."""
        Scanner(self.runtime.dbs, self.runtime.config.pipeline.discovery).scan(self.layout.library)
        self.work()

    def work(self) -> None:
        Worker(self.runtime).drain()
        with self.runtime.dbs.state.write() as conn:
            queue.enqueue(
                conn, SIDECARS, lane="executor", dedupe_key=f"schedule:{SIDECARS}", max_attempts=1
            )
        Worker(self.runtime, lanes=("executor",)).drain()
        scanner = Scanner(self.runtime.dbs, self.runtime.config.pipeline.discovery)
        scanner.scan(self.layout.library)  # sidecars are files too
        Worker(self.runtime).drain()

    def cli(self, *args: str) -> Any:
        env = {"STEWARD_CONFIG_DIR": str(self.tmp_path / "config")}
        result = CliRunner().invoke(cli, [*args], env=env)
        assert result.exit_code == 0, result.output
        return result

    def search(self, query: str) -> list[str]:
        with self.runtime.dbs.state.read() as conn:
            policy = load_policy(conn)
        embed = self.runtime.query_embedder
        hits = search_all(self.runtime.dbs, policy, query, embed=embed).hits
        return [hit.relpath for hit in hits]

    def tags(self) -> dict[str, list[str]]:
        """Effective tags of every file."""
        with self.runtime.dbs.state.read() as state, self.runtime.dbs.index.read() as index:
            policy = load_policy(state)
            rows = state.execute(
                select(files).where(files.c.present.is_(True), files.c.content_hash.is_not(None))
            ).all()
            refs = [
                effective.FileRef(r.file_id, r.content_hash, r.library_id, r.relpath) for r in rows
            ]
            found = effective.many(state, index, policy, refs)
        return {
            r.relpath: sorted(t.name for t in found[r.file_id])
            for r in rows
            if not r.relpath.endswith(".xmp")
        }

    def index_snapshot(self) -> dict[str, Any]:
        """Everything in `index.db` that isn't a timestamp or a row number. Tables are
        found by name, so a new one is compared without changing this."""
        snapshot: dict[str, Any] = {}
        with sqlite3.connect(self.runtime.dbs.index.path) as conn:
            conn.row_factory = sqlite3.Row
            names = [
                r["name"]
                for r in conn.execute("PRAGMA table_list")
                if r["type"] == "table"
                and not r["name"].startswith(("sqlite_", "alembic_version_"))
            ]
            for name in names:
                if name == "embeddings":
                    query = (
                        "SELECT c.content_hash, c.artifact, c.seq, e.vector "
                        "FROM embeddings e JOIN chunks c USING (chunk_id)"
                    )
                else:
                    query = f'SELECT * FROM "{name}"'  # noqa: S608 -- names from the database
                snapshot[name] = sorted(
                    (
                        tuple(
                            (key, row[key])
                            for key in row.keys()  # noqa: SIM118 -- sqlite3.Row
                            if key
                            not in (
                                "chunk_id",
                                "space_id",
                                "created_at",
                                "updated_at",
                                "completed_at",
                            )
                        )
                        for row in conn.execute(query)
                    ),
                    key=repr,
                )
        return snapshot

    def sidecars(self) -> dict[str, bytes]:
        return {str(p): p.read_bytes() for p in self.root.rglob("*.xmp")}

    def journal_size(self) -> int:
        with self.runtime.dbs.state.read() as conn:
            return len(conn.execute(select(journal.c.entry_id)).all())


@pytest.fixture(params=LAYOUTS, ids=lambda layout: layout.name)
def layout(request: pytest.FixtureRequest) -> Layout:
    return request.param  # type: ignore[no-any-return]


@pytest.fixture
def install(tmp_path: Path, layout: Layout) -> Iterator[Install]:
    with model_server() as server:
        env = Install(tmp_path, layout, server)
        yield env
        env.stop()


QUERIES = ("invoice", "receipt", "tenant", "beach", "loaf", "trip", "plumber")


def results(env: Install) -> dict[str, Any]:
    return {
        "search": {q: env.search(q) for q in QUERIES},
        "tags": env.tags(),
        "index": env.index_snapshot(),
    }


# --- a scanned PDF dropped in an inbox folder is searchable within minutes ---------------


def test_a_scanned_pdf_dropped_in_the_inbox_is_searchable_within_a_minute(install: Install) -> None:
    """A real worker and watcher, running in threads, as `steward worker` runs them."""
    runtime = install.runtime
    stop = threading.Event()
    watcher = Watcher(runtime.dbs, runtime.config.pipeline.discovery)
    threads = [
        threading.Thread(target=watcher.run, args=(stop,)),
        threading.Thread(target=Worker(runtime).run, args=(stop,)),
    ]
    for thread in threads:
        thread.start()
    target = install.root / install.layout.inbox / "scan.pdf"
    draft = install.tmp_path / "scan.pdf"
    draft.write_bytes(make_pdf([""], images={1}))
    try:
        time.sleep(1.5)  # the watcher has to be watching
        started = time.monotonic()
        shutil.move(draft, target)
        found: list[str] = []
        while not found and time.monotonic() - started < 60:
            found = install.search(SCANNED_WORD)
            time.sleep(0.1)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=20)
    assert found == [f"{install.layout.inbox}/scan.pdf"]


# --- over 90% of documents and photos get a tag -------------------------------------------


def test_over_90_percent_of_documents_and_photos_get_a_tag(install: Install) -> None:
    tags = install.tags()
    private = install.layout.private + "/"
    taggable = {path: names for path, names in tags.items() if not path.startswith(private)}
    assert len(taggable) >= 9
    tagged = [path for path, names in taggable.items() if names]
    assert len(tagged) / len(taggable) > 0.9, taggable
    assert tags[f"{install.layout.docs}/invoice-1.pdf"] == ["finance/invoice"]
    assert tags[f"{install.layout.docs}/receipt.txt"] == ["finance/receipt"]


# --- your tags survive re-tagging ---------------------------------------------------------


def test_your_tags_survive_retagging(install: Install) -> None:
    docs = install.layout.docs
    library = install.layout.library
    added, removed = f"{docs}/lease.md", f"{docs}/invoice-1.pdf"
    run = install.runtime.executor.run
    run("tag.add", {"path": f"{library}:{added}", "tag": "yours"}, actor=ACTOR, confirmed=True)
    run(
        "tag.remove",
        {"path": f"{library}:{removed}", "tag": "finance/invoice"},
        actor=ACTOR,
        confirmed=True,
    )
    before = install.tags()
    assert before[added] == ["home", "yours"]
    assert before[removed] == []
    calls = len(install.server.chats())

    install.cli("tags", "retag", "--all")
    install.work()
    assert len(install.server.chats()) > calls
    assert install.tags() == before

    install.cli("reindex", "--yes")
    install.work()
    assert install.tags() == before


# --- deleting index.db and reindexing gives the same results ------------------------------


def test_deleting_index_db_and_reindexing_gives_the_same_results(install: Install) -> None:
    before = results(install)
    assert before["index"]["embeddings"]
    assert before["index"]["properties"]
    assert before["index"]["tag_suggestions"]
    sidecars = install.sidecars()
    assert sidecars
    entries = install.journal_size()

    install.stop()
    data = install.tmp_path / "data"
    for name in ("index.db", "index.db-wal", "index.db-shm"):
        (data / name).unlink(missing_ok=True)
    install.cli("reindex", "--yes")
    install.start()
    install.work()

    assert results(install) == before
    assert install.sidecars() == sidecars
    assert install.journal_size() == entries
    with install.runtime.dbs.state.read() as conn:
        assert not reindex.rebuilding(conn)


def test_reindexing_with_everything_running_gives_the_same_results(install: Install) -> None:
    before = results(install)
    sidecars = install.sidecars()
    install.cli("reindex", "--yes")
    install.work()
    after = results(install)
    assert after["search"] == before["search"]
    assert after["tags"] == before["tags"]
    for table, rows in before["index"].items():
        assert after["index"][table] == rows, table
    assert install.sidecars() == sidecars


def test_a_worker_that_finds_the_index_lost_rebuilds_it(install: Install) -> None:
    """Nobody ran `steward reindex`: the API migrated an empty `index.db` at its start."""
    before = results(install)
    sidecars = install.sidecars()
    install.stop()
    data = install.tmp_path / "data"
    for name in ("index.db", "index.db-wal", "index.db-shm"):
        (data / name).unlink(missing_ok=True)
    install.start()
    assert reindex.lost(install.runtime.dbs)
    with install.runtime.dbs.state.write() as conn:
        reindex.request(conn, actor="system")
    install.work()
    assert results(install) == before
    assert install.sidecars() == sidecars


def test_the_rebuild_waits_for_the_model_and_leaves_sidecars_alone(install: Install) -> None:
    before = results(install)
    sidecars = install.sidecars()
    assert any(b"Description" in data for data in sidecars.values())
    install.server.status = 503
    install.cli("reindex", "--yes")
    install.work()
    with install.runtime.dbs.state.read() as conn:
        assert reindex.rebuilding(conn)
        queued = conn.execute(select(jobs.c.job_id).where(jobs.c.status == "queued")).all()
    assert queued
    # Text is back while the model is away; the sidecars haven't noticed anything.
    assert install.search("plumber") == [f"{install.layout.docs}/invoice-1.pdf"]
    assert install.sidecars() == sidecars
    assert "rebuilding since" in install.cli("reindex", "--status").output

    install.server.status = None
    install.runtime.gateway._down.clear()
    with install.runtime.dbs.state.write() as conn:
        conn.execute(
            update(jobs)
            .where(jobs.c.status == "queued")
            .values(run_after=datetime(2020, 1, 1, tzinfo=UTC))
        )
    install.work()
    assert results(install) == before
    assert install.sidecars() == sidecars


# --- the privacy suite is green and a .kdbx in the fixtures is never read ------------------


def test_a_kdbx_is_never_read(tmp_path: Path, layout: Layout) -> None:
    """Not at the first index, not when rebuilding it: no process here opens the file, and
    its content is nowhere afterwards. (The privacy suite in `tests/privacy` is green.)"""
    opened: list[str] = []

    def audit(event: str, args: tuple[Any, ...]) -> None:
        # The fixture writes the file; only reading it counts.
        if event != "open" or not str(args[0]).endswith(".kdbx"):
            return
        if args[2] & os.O_ACCMODE != os.O_WRONLY:
            opened.append(str(args[0]))

    sys.addaudithook(audit)
    with model_server() as server:
        env = Install(tmp_path, layout, server)
        secret = env.root / env.layout.docs / "passwords.kdbx"
        if os.geteuid() != 0:  # also fails loudly if a child process tries
            secret.chmod(0)
        env.cli("reindex", "--yes")
        env.work()
        with env.runtime.dbs.state.read() as conn:
            row = conn.execute(
                select(files).where(files.c.relpath.endswith("passwords.kdbx"))
            ).one()
        env.stop()
        everything = json.dumps(server.requests)
    secret.chmod(0o600)
    assert opened == []
    assert (row.content_hash, row.hash_kind, row.error) == (None, None, None)
    assert row.present
    assert CANARY not in everything
    for path in (tmp_path / "data").rglob("*"):
        if path.is_file():
            assert CANARY.encode() not in path.read_bytes(), path


# --- graceful degradation; back-fill when a model is added --------------------------------


def test_without_a_model_it_works_and_adding_one_fills_everything_in(
    tmp_path: Path, layout: Layout
) -> None:
    with model_server() as server:
        env = Install(tmp_path, layout, server, with_model=False)
        assert env.search("plumber") == [f"{layout.docs}/invoice-1.pdf"]
        assert env.search("beach") == [f"{layout.photos}/beach.jpg"]  # the name
        assert env.tags()[f"{layout.docs}/invoice-1.pdf"] == []
        assert server.requests == []
        assert env.sidecars() == {}

        env.stop()
        env.with_model = True
        env.write_config()
        env.start()
        env.work()
        assert env.tags()[f"{layout.docs}/invoice-1.pdf"] == ["finance/invoice"]
        assert env.search("waves")  # a description, written by the vision model
        assert env.search("how to fix a faucet")  # meaning: "plumber" embeds as "faucet"
        env.stop()
