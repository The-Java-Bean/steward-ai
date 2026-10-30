# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Rebuilding the index, detail by detail (the end-to-end results are in
`tests/acceptance/test_phase3.py`): emptying `index.db` in place, the scanner restoring
what it lost without reading more than it must, a stage caught in the middle, when a
rebuild counts as finished, and the command."""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DatabaseError
from support import eps, make_module, write_config
from typer.testing import CliRunner

from steward.cli import app as cli_app
from steward.core import backup, reindex, settings, stages
from steward.core.config import load_config
from steward.core.db import migrate
from steward.core.db.schema import blobs, chunks, events, files, jobs, stage_runs, texts
from steward.core.discovery import Scanner
from steward.core.discovery.jobs import enqueue_full_scan
from steward.core.folders import FolderRef, load_policy, store
from steward.core.privacy import set_override
from steward.core.runtime import Runtime, build_runtime
from steward.core.search import SearchResults, search_all
from steward.core.worker import Worker
from steward.modules.documents import DocumentsModule
from steward.sdk import JobContext, PermanentJobError

ACTOR = "user:1"
TREE = {
    "docs/one.txt": "the first plain words\n",
    "docs/two.txt": "the second plain words\n",
    "docs/copy-of-two.txt": "the second plain words\n",
    "docs/keys.kdbx": "secret",
    "docs/hidden-by-rule.txt": "not to be read\n",
}


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.root = tmp_path / "lib"
        for relpath, content in TREE.items():
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.config_dir = write_config(
            tmp_path / "config",
            steward={"data_dir": str(tmp_path / "data")},
            # The CLI loads every built-in module that isn't turned off.
            modules={
                "modules": {
                    "documents": {"enabled": True},
                    **{
                        name: {"enabled": False}
                        for name in ("fields", "files_ui", "photos", "search", "shares", "tags")
                    },
                }
            },
            pipeline={"schedules": {"core.reconcile": "off"}},
        )
        self.runtime = self.build()
        self.runtime.upgrade_databases()
        with self.runtime.dbs.state.write() as conn:
            store.upsert_library(conn, "lib", root=str(self.root), actor=ACTOR)
            store.set_type(conn, FolderRef("lib", "docs"), "documents", actor=ACTOR)
        self.scan()

    def build(self) -> Runtime:
        return build_runtime(load_config(self.config_dir), entry_points=eps(DocumentsModule))

    def scan(self, paths: tuple[str, ...] = ("",)) -> Any:
        report = Scanner(self.runtime.dbs, self.runtime.config.pipeline.discovery).scan(
            "lib", paths
        )
        Worker(self.runtime).drain()
        return report

    def rebuild(self) -> None:
        with self.runtime.dbs.state.write() as conn:
            reindex.request(conn, actor="test")
        Worker(self.runtime).drain()

    def row(self, relpath: str) -> Any:
        with self.runtime.dbs.state.read() as conn:
            return conn.execute(select(files).where(files.c.relpath == relpath)).one()

    def search(self, query: str) -> list[str]:
        with self.runtime.dbs.state.read() as conn:
            policy = load_policy(conn)
        return [h.relpath for h in search_all(self.runtime.dbs, policy, query).hits]

    def count(self, table: Any) -> int:
        with self.runtime.dbs.index.read() as conn:
            return len(conn.execute(select(table)).all())

    def rebuilding(self) -> bool:
        with self.runtime.dbs.state.read() as conn:
            return reindex.rebuilding(conn)

    def event_types(self) -> list[str]:
        with self.runtime.dbs.state.read() as conn:
            return list(conn.execute(select(events.c.type).order_by(events.c.event_id)).scalars())

    def cli(self, *args: str, input: str | None = None) -> Any:
        return CliRunner().invoke(
            cli_app,
            list(args),
            env={"STEWARD_CONFIG_DIR": str(self.config_dir)},
            input=input,
        )


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = Env(tmp_path)
    yield environment
    environment.runtime.close()


# --- emptying index.db ------------------------------------------------------------------


def test_reset_empties_every_table_and_keeps_the_schema(env: Env) -> None:
    db = env.runtime.dbs.index
    env_contents = env.count(blobs)
    with db.read() as conn:
        schema = conn.execute(text("SELECT sql FROM sqlite_master ORDER BY name")).all()
    assert env.count(chunks)
    assert env.count(blobs)
    removed = reindex.reset(db)
    assert removed["blobs"] == env_contents
    assert removed["chunks"]
    assert env.count(chunks) == env.count(blobs) == env.count(texts) == env.count(stage_runs) == 0
    with db.read() as conn:
        assert conn.execute(text("SELECT sql FROM sqlite_master ORDER BY name")).all() == schema
        assert conn.exec_driver_sql("SELECT count(*) FROM chunks_fts").scalar() == 0
    migrate.ensure_current(db, env.runtime.migration_owners("index"))


def test_reset_leaves_a_search_index_that_still_works(env: Env) -> None:
    reindex.reset(env.runtime.dbs.index)
    assert env.search("plain") == []
    (env.root / "docs/three.txt").write_text("the third plain words\n")
    env.scan()
    assert "docs/three.txt" in env.search("third")
    assert "docs/one.txt" in env.search("first")
    with sqlite3.connect(env.runtime.dbs.index.path) as conn:
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('integrity-check')")


def test_reset_includes_tables_of_modules_and_of_ones_turned_off(env: Env) -> None:
    with env.runtime.dbs.index.write() as conn:
        conn.execute(text("CREATE TABLE other_things (a INTEGER)"))
        conn.execute(text("INSERT INTO other_things VALUES (1)"))
    assert reindex.reset(env.runtime.dbs.index)["other_things"] == 1


def test_a_full_scan_after_losing_the_index_brings_everything_back(env: Env) -> None:
    before = env.search("plain")
    with env.runtime.dbs.index.read() as conn:
        rows_before = sorted(
            tuple(r) for r in conn.execute(select(texts.c.content_hash, texts.c.body))
        )
    reindex.reset(env.runtime.dbs.index)
    assert env.search("first") == []
    report = env.scan()
    assert report.files_restored == 3  # three contents; the copy of two needed no reading
    assert report.files_changed == 0
    assert sorted(env.search("plain")) == sorted(before)
    with env.runtime.dbs.index.read() as conn:
        assert (
            sorted(tuple(r) for r in conn.execute(select(texts.c.content_hash, texts.c.body)))
            == rows_before
        )


def test_only_contents_that_are_lost_are_read_again(env: Env) -> None:
    with env.runtime.dbs.index.write() as conn:
        conn.execute(
            blobs.delete().where(blobs.c.content_hash == env.row("docs/one.txt").content_hash)
        )
    assert env.scan().files_restored == 1
    assert env.event_types().count("file.reindexed") == 1
    assert env.scan().files_restored == 0


def test_a_scan_of_one_folder_restores_only_that_folder(env: Env) -> None:
    (env.root / "other").mkdir()
    (env.root / "other/three.txt").write_text("words elsewhere\n")
    env.scan()
    reindex.reset(env.runtime.dbs.index)
    assert env.scan(("other",)).files_restored == 1


def test_excluded_files_stay_unread_when_restoring(env: Env) -> None:
    with env.runtime.dbs.state.write() as conn:
        set_override(conn, env.row("docs/hidden-by-rule.txt").file_id, "excluded", actor=ACTOR)
    env.scan()
    assert env.row("docs/hidden-by-rule.txt").content_hash is None
    assert env.row("docs/keys.kdbx").content_hash is None
    reindex.reset(env.runtime.dbs.index)
    if os.geteuid() != 0:
        (env.root / "docs/hidden-by-rule.txt").chmod(0)
        (env.root / "docs/keys.kdbx").chmod(0)
    report = env.scan()
    (env.root / "docs/hidden-by-rule.txt").chmod(0o600)
    (env.root / "docs/keys.kdbx").chmod(0o600)
    assert report.errors == 0
    assert env.row("docs/hidden-by-rule.txt").error is None
    assert env.row("docs/keys.kdbx").content_hash is None


def test_content_that_changed_without_its_mtime_is_a_change_not_a_restore(env: Env) -> None:
    path = env.root / "docs/one.txt"
    stat = path.stat()
    path.write_text("the FIRST plain words\n")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    env.scan()  # nothing noticed: size, time and inode are the same
    assert env.search("first") == ["docs/one.txt"]
    reindex.reset(env.runtime.dbs.index)
    report = env.scan()
    assert report.files_changed == 1
    assert "file.changed" in env.event_types()
    assert env.search("first") == ["docs/one.txt"]


def test_a_touched_file_is_restored_too(env: Env) -> None:
    path = env.root / "docs/one.txt"
    reindex.reset(env.runtime.dbs.index)
    os.utime(path, (1_000_000_000, 1_000_000_000))
    report = env.scan()
    assert report.files_restored == 3
    assert env.search("first") == ["docs/one.txt"]


def test_a_file_that_comes_back_after_a_rebuild_is_restored(env: Env) -> None:
    path = env.root / "docs/one.txt"
    kept = env.tmp_path / "kept.txt"
    stat = path.stat()
    shutil.move(path, kept)
    env.scan()
    assert not env.row("docs/one.txt").present
    env.rebuild()
    assert env.search("first") == []
    shutil.move(kept, path)
    assert path.stat().st_ino == stat.st_ino
    report = env.scan()
    assert report.files_reappeared == 1
    assert env.search("first") == ["docs/one.txt"]


def test_a_copy_is_tried_when_the_first_file_cant_be_read(env: Env) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    reindex.reset(env.runtime.dbs.index)
    (env.root / "docs/copy-of-two.txt").chmod(0)
    report = env.scan()
    (env.root / "docs/copy-of-two.txt").chmod(0o600)
    assert report.files_restored == 3
    assert report.errors == 1
    found = reindex.summary(env.runtime.dbs, env.runtime.stages)
    assert found.indexed == found.contents == 3


# --- the job -----------------------------------------------------------------------------


def test_the_job_rebuilds_and_the_mark_goes_away(env: Env) -> None:
    env.rebuild()
    assert not env.rebuilding()
    assert sorted(env.search("plain")) == ["docs/copy-of-two.txt", "docs/one.txt", "docs/two.txt"]
    with env.runtime.dbs.state.read() as conn:
        statuses = conn.execute(select(jobs.c.status).where(jobs.c.lane != "executor")).scalars()
        assert set(statuses) <= {"done"}


def test_search_says_when_files_are_missing_because_of_a_rebuild(env: Env) -> None:
    def results() -> SearchResults:
        with env.runtime.dbs.state.read() as conn:
            policy = load_policy(conn)
        return search_all(env.runtime.dbs, policy, "plain")

    assert not results().rebuilding
    with env.runtime.dbs.state.write() as conn:
        reindex.request(conn, actor="test")
    assert results().rebuilding
    Worker(env.runtime).drain()
    assert not results().rebuilding


def test_a_scan_already_running_doesnt_stand_in_for_the_rebuilds_own(env: Env) -> None:
    """It may have gone past files before the reset."""
    with env.runtime.dbs.state.write() as conn:
        running = enqueue_full_scan(conn, "lib")
        conn.execute(jobs.update().where(jobs.c.job_id == running).values(status="running"))
        reindex.request(conn, actor="test")
    worker = Worker(env.runtime)
    while worker.run_one("cpu"):
        pass
    with env.runtime.dbs.state.read() as conn:
        scans = conn.execute(select(jobs.c.dedupe_key).where(jobs.c.status == "queued")).scalars()
        assert "reindex:lib" in set(scans)
    with env.runtime.dbs.state.write() as conn:
        conn.execute(jobs.update().where(jobs.c.job_id == running).values(status="done"))


def test_the_rebuild_isnt_over_while_the_planner_has_events_to_read(env: Env) -> None:
    with env.runtime.dbs.state.write() as conn:
        reindex.request(conn, actor="test")
    worker = Worker(env.runtime)
    assert not reindex.settle(env.runtime.dbs)  # the job is queued
    while worker.run_one("cpu"):
        pass
    assert not reindex.settle(env.runtime.dbs)  # scans are queued
    while worker.run_one("io"):
        pass
    with env.runtime.dbs.state.read() as conn:
        assert stages.plan_pending(conn)
    assert not reindex.settle(env.runtime.dbs)
    worker.drain()
    assert not env.rebuilding()


def test_a_stage_running_during_the_reset_leaves_nothing_wrong(tmp_path: Path) -> None:
    """The reset lands between a stage reading its input and writing its result."""
    from steward.sdk import Registry, StageContext, StageResult

    ran: list[str] = []
    holder: list[Runtime] = []

    def probe_stage(ctx: StageContext) -> StageResult:
        ran.append(ctx.blob.content_hash)
        if len(ran) == 1:
            reindex.reset(holder[0].dbs.index)
        ctx.write_text([], artifact="probe")
        return StageResult()

    def register(r: Registry) -> None:
        r.stage("probe.run", probe_stage, version=1, requires=("mime",), produces=("probe",))

    module = make_module("probe", register=register)
    root = tmp_path / "lib"
    root.mkdir()
    (root / "a.txt").write_text("alpha\n")
    config_dir = write_config(
        tmp_path / "config",
        steward={"data_dir": str(tmp_path / "data")},
        modules={"modules": {"probe": {"enabled": True}}},
        pipeline={"schedules": {"core.reconcile": "off"}},
    )
    runtime = build_runtime(load_config(config_dir), entry_points=eps(module))
    holder.append(runtime)
    runtime.upgrade_databases()
    with runtime.dbs.state.write() as conn:
        store.upsert_library(conn, "lib", root=str(root), actor=ACTOR)
    Scanner(runtime.dbs, runtime.config.pipeline.discovery).scan("lib")
    Worker(runtime).drain()  # fails on the foreign key: the blob is gone
    Scanner(runtime.dbs, runtime.config.pipeline.discovery).scan("lib")
    with runtime.dbs.state.write() as conn:
        conn.execute(jobs.update().where(jobs.c.status == "queued").values(run_after=_past()))
    Worker(runtime).drain()
    with runtime.dbs.index.read() as conn:
        runs = {r.stage: r.status for r in conn.execute(select(stage_runs))}
    runtime.close()
    assert runs == {"core.hash": "done", "probe.run": "done"}
    assert len(ran) >= 2


def _past() -> Any:
    from datetime import UTC, datetime

    return datetime(2020, 1, 1, tzinfo=UTC)


def _context() -> JobContext:
    return JobContext(1, reindex.REBUILD_JOB, {}, 1, lambda: None, None)  # type: ignore[arg-type]


def test_nothing_else_starts_while_the_index_is_being_emptied(env: Env, monkeypatch: Any) -> None:
    """Emptying can take minutes, far longer than a writer waits for the lock."""
    claimed: list[bool] = []
    original = reindex.reset

    def reset(db: Any) -> dict[str, int]:
        with env.runtime.dbs.state.write() as conn:
            enqueue_full_scan(conn, "lib")
        claimed.append(Worker(env.runtime).run_one("io"))
        return original(db)

    monkeypatch.setattr(reindex, "reset", reset)
    with env.runtime.dbs.state.read() as conn:
        assert reindex.claimable(conn, ["core.scan"]) == ["core.scan"]
    env.rebuild()
    assert claimed == [False]
    assert env.search("first") == ["docs/one.txt"]
    assert not env.rebuilding()


def test_a_hard_kill_while_emptying_doesnt_stop_everything_for_good(env: Env) -> None:
    with env.runtime.dbs.state.write() as conn:
        settings.set_value(conn, reindex.SETTING, {"emptying": True}, actor="test")
        # The job isn't there any more: it used up its attempts, or never was.
        assert reindex.claimable(conn, ["core.scan"]) == ["core.scan"]
    assert reindex.settle(env.runtime.dbs)


def test_a_killed_rebuild_runs_again(env: Env) -> None:
    with env.runtime.dbs.state.write() as conn:
        reindex.request(conn, actor="test")
        settings.set_value(conn, reindex.SETTING, {"emptying": True}, actor="test")
    Worker(env.runtime).drain()
    assert not env.rebuilding()
    assert env.search("first") == ["docs/one.txt"]


def test_the_lease_is_kept_while_emptying(env: Env, monkeypatch: Any) -> None:
    renewed = threading.Event()
    monkeypatch.setattr(reindex, "LEASE_RENEW_S", 0.01)
    original = reindex.reset

    def slow(db: Any) -> dict[str, int]:
        assert renewed.wait(5)
        return original(db)

    monkeypatch.setattr(reindex, "reset", slow)
    context = JobContext(1, reindex.REBUILD_JOB, {}, 1, renewed.set, None)  # type: ignore[arg-type]
    env.runtime.registrations.job_types()[reindex.REBUILD_JOB].handler(context)
    assert not threading.enumerate()[-1].name.startswith("reindex-lease")


def test_a_failed_reset_doesnt_leave_everyone_waiting(env: Env, monkeypatch: Any) -> None:
    def busy(db: Any) -> dict[str, int]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(reindex, "reset", busy)
    with env.runtime.dbs.state.write() as conn:
        reindex.request(conn, actor="test")
    with pytest.raises(sqlite3.OperationalError):
        env.runtime.registrations.job_types()[reindex.REBUILD_JOB].handler(_context())
    with env.runtime.dbs.state.read() as conn:
        assert reindex.claimable(conn, ["core.scan"]) == ["core.scan"]


def test_a_damaged_index_fails_with_the_way_out(env: Env) -> None:
    env.runtime.dbs.index.dispose()
    path = env.runtime.dbs.index.path
    for suffix in ("-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    path.write_bytes(b"this isn't a database" * 1000)
    handler = env.runtime.registrations.job_types()[reindex.REBUILD_JOB].handler
    with pytest.raises(PermanentJobError, match="delete index.db"):
        handler(_context())


@pytest.mark.parametrize(
    "command", [("reindex", "--yes"), ("worker",), ("db", "upgrade"), ("files", "list")]
)
def test_commands_name_the_way_out_of_a_damaged_index(env: Env, command: tuple[str, ...]) -> None:
    """An unreadable file stops everything at startup, before any job can run."""
    env.runtime.dbs.index.dispose()
    path = env.runtime.dbs.index.path
    for suffix in ("-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    path.write_bytes(b"this isn't a database" * 1000)
    result = env.cli(*command)
    assert result.exit_code == 1
    assert "index.db is damaged" in result.output
    assert "delete index.db" in result.output


def test_a_damaged_state_db_isnt_blamed_on_the_index(env: Env) -> None:
    env.runtime.dbs.state.dispose()
    path = env.runtime.dbs.state.path
    for suffix in ("-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    path.write_bytes(b"this isn't a database" * 1000)
    with pytest.raises(DatabaseError):
        env.runtime.upgrade_databases()


def test_a_busy_index_is_retried_not_called_damaged(env: Env, monkeypatch: Any) -> None:
    def busy(db: Any) -> dict[str, int]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(reindex, "reset", busy)
    handler = env.runtime.registrations.job_types()[reindex.REBUILD_JOB].handler
    with pytest.raises(sqlite3.OperationalError):
        handler(_context())


def test_lost_tells_a_lost_index_from_a_new_install(env: Env, tmp_path: Path) -> None:
    assert not reindex.lost(env.runtime.dbs)
    reindex.reset(env.runtime.dbs.index)
    assert reindex.lost(env.runtime.dbs)


# --- the command --------------------------------------------------------------------------


def test_the_command_asks_says_what_happens_and_takes_a_backup(env: Env) -> None:
    result = env.cli("reindex", input="n\n")
    assert result.exit_code == 1
    assert "computed again: documents.extract" in result.output
    assert not env.rebuilding()
    assert backup.list_backups(env.runtime.config.steward.backup_dir) == []

    result = env.cli("reindex", input="y\n")
    assert result.exit_code == 0, result.output
    assert env.rebuilding()
    infos = backup.list_backups(env.runtime.config.steward.backup_dir)
    assert [i.reason for i in infos] == ["pre-reindex"]
    assert "--status" in result.output

    result = env.cli("reindex", "--yes")
    assert result.exit_code == 1
    assert "already running" in result.output

    assert "rebuilding since" in env.cli("reindex", "--status").output
    Worker(env.runtime).drain()
    assert "not rebuilding: 3 of 3 contents" in env.cli("reindex", "--status").output


def test_no_backup_skips_it(env: Env) -> None:
    assert env.cli("reindex", "--yes", "--no-backup").exit_code == 0
    assert backup.list_backups(env.runtime.config.steward.backup_dir) == []


def test_the_command_refuses_a_missing_file_with_its_journal_still_there(env: Env) -> None:
    env.runtime.dbs.index.dispose()
    path = env.runtime.dbs.index.path
    path.unlink()
    path.with_name("index.db-wal").write_bytes(b"stale")
    result = env.cli("reindex", "--yes")
    assert result.exit_code == 1
    assert "index.db-wal" in result.output
    assert "Stop the API" in result.output
    assert not path.exists()


def test_the_command_creates_a_missing_file(env: Env) -> None:
    env.runtime.dbs.index.dispose()
    path = env.runtime.dbs.index.path
    for name in ("index.db", "index.db-wal", "index.db-shm"):
        path.with_name(name).unlink(missing_ok=True)
    result = env.cli("reindex", "--yes")
    assert result.exit_code == 0, result.output
    assert "backup:" not in result.output
    assert path.exists()
    Worker(env.runtime).drain()
    assert not env.rebuilding()
    assert env.search("first") == ["docs/one.txt"]


def test_the_command_says_what_wont_come_back(env: Env) -> None:
    with env.runtime.dbs.index.write() as conn:
        conn.execute(
            stage_runs.insert().values(
                content_hash=env.row("docs/one.txt").content_hash,
                stage="gone.stage",
                stage_version=1,
                status="done",
                updated_at=_past(),
            )
        )
    graph = env.runtime.stages
    found = reindex.summary(env.runtime.dbs, graph)
    assert "gone.stage" in found.results
    object.__setattr__(graph, "inactive", {**graph.inactive, "gone.stage": "role 'x' isn't mapped"})
    assert reindex.summary(env.runtime.dbs, graph).not_running == {
        "gone.stage": "role 'x' isn't mapped"
    }


def test_the_health_check_reports_a_rebuild(env: Env) -> None:
    import asyncio

    check = env.runtime.registrations.health_checks()["core.index"]
    assert asyncio.run(check()).status == "ok"
    with env.runtime.dbs.state.write() as conn:
        reindex.request(conn, actor="test")
    result = asyncio.run(check())
    assert result.status == "warn"
    assert "rebuilding" in (result.detail or "")
