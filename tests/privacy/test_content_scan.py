# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Part of the privacy suite: a credential inside an ordinary file (docs/adr/0029). Its
text must reach neither the index, nor a model, nor the logs."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from model_server import FakeModelServer, llm_config, model_server
from sqlalchemy import func, select
from support import eps, make_module, write_config
from typer.testing import CliRunner

from steward.cli import app as cli_app
from steward.core import reindex
from steward.core.config import load_config
from steward.core.db.schema import blobs, chunks, file_sensitivity, files, stage_runs, texts
from steward.core.discovery import Scanner
from steward.core.folders import FolderRef, load_policy, store
from steward.core.privacy import SCAN_ACTOR, held_back, set_override
from steward.core.runtime import Runtime, build_runtime
from steward.core.search import search_all
from steward.core.stages.planner import Planner
from steward.core.worker import Worker
from steward.modules.documents import DocumentsModule
from steward.modules.search import SearchModule
from steward.sdk import Registry, StageContext, StageResult, TextPart

ACTOR = "user:1"
VALUE = "Tr0ub4dor3xZebra"
LEAKY = f"service: mail\nwords only here zebrafinch\nsmtp_password: {VALUE}\n"
CLEAN = "service: mail\nwords only here zebrafinch\nsmtp_password: ${MAIL_PASSWORD}\n"
TREE = {
    "docs/mail.yml": LEAKY,
    "docs/copy-of-mail.yml": LEAKY,
    "docs/plain.txt": "ordinary words about kestrels\n",
    "samples/mail.yml": LEAKY + "# a sample\n",
}


class Env:
    def __init__(self, tmp_path: Path, server: FakeModelServer | None, **privacy: Any) -> None:
        self.root = tmp_path / "lib"
        for relpath, content in TREE.items():
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        extra = {"llm": llm_config(server.url)} if server is not None else {}
        self.config_dir = write_config(
            tmp_path / "config",
            steward={"data_dir": str(tmp_path / "data")},
            modules={
                "modules": {
                    "documents": {"enabled": True},
                    "search": {"enabled": True},
                    **{
                        name: {"enabled": False}
                        for name in ("fields", "files_ui", "photos", "shares", "tags")
                    },
                }
            },
            pipeline={"schedules": {"core.reconcile": "off"}},
            privacy=privacy,
            **extra,
        )
        self.runtime = self.build()
        self.runtime.upgrade_databases()
        with self.runtime.dbs.state.write() as conn:
            store.upsert_library(conn, "lib", root=str(self.root), actor=ACTOR)
            store.set_type(conn, FolderRef("lib", ""), "documents", actor=ACTOR)
        self.scan()

    def build(self) -> Runtime:
        return build_runtime(
            load_config(self.config_dir), entry_points=eps(DocumentsModule, SearchModule)
        )

    def scan(self) -> None:
        Scanner(self.runtime.dbs, self.runtime.config.pipeline.discovery).scan("lib")
        Worker(self.runtime).drain()

    def row(self, relpath: str) -> Any:
        with self.runtime.dbs.state.read() as conn:
            return conn.execute(select(files).where(files.c.relpath == relpath)).one()

    def search(self, query: str) -> list[str]:
        with self.runtime.dbs.state.read() as conn:
            policy = load_policy(conn)
        return [h.relpath for h in search_all(self.runtime.dbs, policy, query).hits]

    def index_text(self) -> str:
        with self.runtime.dbs.index.read() as conn:
            rows = conn.execute(select(texts.c.body)).scalars().all()
            more = conn.execute(select(chunks.c.body)).scalars().all()
        return "\n".join([*rows, *more])


@pytest.fixture
def server() -> Iterator[FakeModelServer]:
    with model_server() as s:
        yield s


@pytest.fixture
def env(tmp_path: Path, server: FakeModelServer) -> Iterator[Env]:
    e = Env(tmp_path, server)
    yield e
    e.runtime.close()


def test_a_credential_never_reaches_the_index_or_a_model(env: Env, server: FakeModelServer) -> None:
    assert VALUE not in env.index_text()
    assert "zebrafinch" not in env.index_text()
    assert not any("zebrafinch" in text or VALUE in text for text in server.embedded())
    assert env.search("zebrafinch") == []
    assert env.search(VALUE) == []


def test_every_copy_is_held_back_and_its_name_stays_findable(env: Env) -> None:
    for relpath in ("docs/mail.yml", "docs/copy-of-mail.yml"):
        assert env.row(relpath).content_hash is None
    with env.runtime.dbs.state.read() as conn:
        found = held_back(conn)
        actors = set(conn.execute(select(file_sensitivity.c.actor)).scalars())
    assert set(found.values()) == {"password value"}
    assert actors == {f"{SCAN_ACTOR}:password value"}
    assert "docs/mail.yml" in env.search("mail")


def test_other_files_are_indexed_as_usual(env: Env, server: FakeModelServer) -> None:
    assert env.search("kestrels") == ["docs/plain.txt"]
    assert any("kestrels" in text for text in server.embedded())


def test_nothing_derived_stays_behind(env: Env) -> None:
    with env.runtime.dbs.index.read() as conn:
        assert conn.execute(select(func.count()).select_from(blobs)).scalar_one() == 1
        hashes = set(conn.execute(select(stage_runs.c.content_hash)).scalars())
    assert hashes == {env.row("docs/plain.txt").content_hash}


def test_logs_name_the_kind_of_credential_only(
    tmp_path: Path, server: FakeModelServer, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        env = Env(tmp_path, server)
        env.runtime.close()
    assert "held back" in caplog.text
    assert VALUE not in caplog.text
    assert "zebrafinch" not in caplog.text


def test_a_file_that_gets_fixed_is_read_again(env: Env) -> None:
    (env.root / "docs/mail.yml").write_text(CLEAN)
    env.scan()
    assert env.row("docs/mail.yml").content_hash is not None
    assert "docs/mail.yml" in env.search("zebrafinch")
    with env.runtime.dbs.state.read() as conn:
        assert env.row("docs/mail.yml").file_id not in held_back(conn)
        assert env.row("docs/copy-of-mail.yml").file_id in held_back(conn)


def test_an_unchanged_file_stays_held_back(env: Env) -> None:
    env.scan()
    assert env.row("docs/mail.yml").content_hash is None
    assert env.search("zebrafinch") == []


def test_a_copy_gets_held_back_when_it_appears_later(env: Env) -> None:
    (env.root / "docs/third.yml").write_text(LEAKY)
    env.scan()
    assert env.row("docs/third.yml").content_hash is None
    assert VALUE not in env.index_text()


def test_paths_can_be_left_out_for_false_positives(tmp_path: Path, server: FakeModelServer) -> None:
    env = Env(tmp_path, server, content_scan_ignore=["samples/**"])
    assert env.row("samples/mail.yml").content_hash is not None
    assert env.row("docs/mail.yml").content_hash is None
    assert env.search("sample") == ["samples/mail.yml"]
    env.runtime.close()


def test_a_copy_inside_an_ignored_path_is_indexed_the_other_held_back(
    tmp_path: Path, server: FakeModelServer
) -> None:
    (tmp_path / "lib" / "samples").mkdir(parents=True)
    (tmp_path / "lib" / "samples" / "same.yml").write_text(LEAKY)
    env = Env(tmp_path, server, content_scan_ignore=["samples/**"])
    assert env.row("docs/mail.yml").content_hash is None
    assert env.row("samples/same.yml").content_hash is not None
    assert sorted(env.search("zebrafinch")) == ["samples/mail.yml", "samples/same.yml"]
    env.runtime.close()


def test_a_copy_outside_an_ignored_path_is_scanned_even_if_the_ignored_one_comes_first(
    tmp_path: Path, server: FakeModelServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Planner.present_files

    def ignored_first(conn: Any, hashes: Any) -> dict[str, list[Any]]:
        return {
            content: sorted(rows, key=lambda f: not f.relpath.startswith("samples/"))
            for content, rows in original(conn, hashes).items()
        }

    monkeypatch.setattr(Planner, "present_files", staticmethod(ignored_first))
    (tmp_path / "lib" / "samples").mkdir(parents=True)
    (tmp_path / "lib" / "samples" / "same.yml").write_text(LEAKY)
    env = Env(tmp_path, server, content_scan_ignore=["samples/**"])
    assert env.row("docs/mail.yml").content_hash is None
    env.runtime.close()


def test_clearing_after_ignoring_a_path_lets_it_be_indexed(
    tmp_path: Path, server: FakeModelServer
) -> None:
    env = Env(tmp_path, server)
    env.runtime.close()
    write_config(
        tmp_path / "config",
        steward={"data_dir": str(tmp_path / "data")},
        modules={
            "modules": {
                "documents": {"enabled": True},
                "search": {"enabled": True},
                **{
                    name: {"enabled": False}
                    for name in ("fields", "files_ui", "photos", "shares", "tags")
                },
            }
        },
        pipeline={"schedules": {"core.reconcile": "off"}},
        privacy={"content_scan_ignore": ["docs/mail.yml"]},
        llm=llm_config(server.url),
    )
    env.runtime = env.build()
    with env.runtime.dbs.state.write() as conn:
        set_override(conn, env.row("docs/mail.yml").file_id, None, actor=ACTOR)
    env.scan()
    assert env.row("docs/mail.yml").content_hash is not None
    env.runtime.close()


def test_the_scan_can_be_turned_off(tmp_path: Path, server: FakeModelServer) -> None:
    env = Env(tmp_path, server, content_scan=False)
    assert env.row("docs/mail.yml").content_hash is not None
    assert VALUE in env.index_text()
    env.runtime.close()


def test_a_rebuild_doesnt_read_it_again(env: Env) -> None:
    with env.runtime.dbs.state.write() as conn:
        reindex.request(conn, actor="test")
    Worker(env.runtime).drain()
    assert env.row("docs/mail.yml").content_hash is None
    assert VALUE not in env.index_text()
    assert env.search("kestrels") == ["docs/plain.txt"]


def test_a_stage_of_any_module_is_covered(tmp_path: Path) -> None:
    """The scan is in `write_text`, so a module gets it without asking."""

    def notes(ctx: StageContext) -> StageResult:
        ctx.write_text([TextPart(f"note about it: password={VALUE}")], artifact="text:notes")
        return StageResult()

    def register(r: Registry) -> None:
        r.stage("notes.write", notes, version=1, requires=("mime",), produces=("text:notes",))

    module = make_module("notes", register=register)
    root = tmp_path / "lib"
    root.mkdir()
    (root / "a.txt").write_text("harmless\n")
    config_dir = write_config(
        tmp_path / "config",
        steward={"data_dir": str(tmp_path / "data")},
        modules={"modules": {"notes": {"enabled": True}}},
        pipeline={"schedules": {"core.reconcile": "off"}},
    )
    runtime = build_runtime(load_config(config_dir), entry_points=eps(module))
    runtime.upgrade_databases()
    with runtime.dbs.state.write() as conn:
        store.upsert_library(conn, "lib", root=str(root), actor=ACTOR)
    Scanner(runtime.dbs, runtime.config.pipeline.discovery).scan("lib")
    Worker(runtime).drain()
    with runtime.dbs.state.read() as conn:
        assert len(held_back(conn)) == 1
    with runtime.dbs.index.read() as conn:
        assert conn.execute(select(func.count()).select_from(texts)).scalar_one() == 0
    runtime.close()


def test_the_command_lists_them_and_health_warns(env: Env, monkeypatch: Any) -> None:
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(env.config_dir))
    runner = CliRunner()
    listed = runner.invoke(cli_app, ["files", "held-back"])
    assert listed.exit_code == 0, listed.output
    assert "lib:docs/mail.yml  (password value)" in listed.output
    assert VALUE not in listed.output
    info = runner.invoke(cli_app, ["files", "info", "lib:docs/mail.yml"])
    assert "held back" in info.output
    assert "(password value)" in info.output
    assert "lib:docs/plain.txt" not in listed.output
