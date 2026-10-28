# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The stage framework end to end: discovery -> planner -> stage jobs -> stage_runs."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from steward_hello import HelloModule
from support import FakeEntryPoint, make_module, make_runtime, write_config

from steward.core import queue
from steward.core.config import load_config
from steward.core.db.schema import stage_runs
from steward.core.discovery import SCAN_JOB, enqueue_full_scan
from steward.core.folders import FolderRef, TypeDefinition, store
from steward.core.loader import RegistrationError
from steward.core.runtime import Runtime, build_runtime
from steward.core.stages import (
    FULL_JOB,
    StageGraphError,
    enqueue_full_pass,
    full_pass_needed,
    stage_job_type,
)
from steward.core.worker import Worker
from steward.sdk import (
    BlobInfo,
    FileInfo,
    FolderInfo,
    PermanentJobError,
    Registry,
    StageContext,
    StageResult,
)

ACTOR = "user:1"


class Recorder:
    """Stages that record what they saw. `behaviour[name]` can change what they do."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.behaviour: dict[str, Callable[[StageContext], StageResult | None]] = {}

    def stage(self, name: str) -> Callable[[StageContext], StageResult | None]:
        def run(ctx: StageContext) -> StageResult | None:
            with ctx.open() as f:
                content = f.read().decode()
            self.calls.append((name, content))
            special = self.behaviour.get(name)
            return special(ctx) if special else None

        return run


def text_only(blob: BlobInfo, file: FileInfo, folder: FolderInfo) -> bool:
    return blob.kind == "text"


def pipeline_module(rec: Recorder, *, version: int = 1) -> type:
    def register(r: Registry) -> None:
        r.stage(
            "pipe.extract",
            rec.stage("pipe.extract"),
            version=version,
            requires=("mime",),
            produces=("text",),
            applies_to=text_only,
        )
        r.stage(
            "pipe.chunk",
            rec.stage("pipe.chunk"),
            version=1,
            requires=("text",),
            produces=("chunks",),
        )
        r.stage(
            "pipe.names",
            rec.stage("pipe.names"),
            version=1,
            requires=("hash",),
            produces=("names",),
            index="metadata",
        )
        r.stage(
            "pipe.extra",
            rec.stage("pipe.extra"),
            version=1,
            requires=("mime",),
            produces=("extra",),
            default=False,
        )

    return make_module("pipe", register=register)


class Env:
    def __init__(self, tmp_path: Path, modules: list[type]) -> None:
        self.tmp_path = tmp_path
        self.root = tmp_path / "lib"
        self.root.mkdir()
        (self.root / ".keep").write_text("")
        pipeline = {
            "discovery": {"ignore_patterns": [".keep"]},
            "schedules": {"core.reconcile": "off"},
        }
        self.runtime: Runtime = make_runtime(tmp_path, modules, pipeline=pipeline)
        with self.runtime.dbs.state.write() as conn:
            store.upsert_library(conn, "main", root=str(self.root), actor=ACTOR)

    def write(self, relpath: str, content: str) -> Path:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def scan(self) -> int:
        """Rescan everything and run all resulting work."""
        with self.runtime.dbs.state.write() as conn:
            enqueue_full_scan(conn, "main")
        return self.drain()

    def drain(self) -> int:
        return Worker(self.runtime).drain()

    def runs(self) -> dict[tuple[str, str], tuple[int, str, str | None]]:
        """(stage, first 8 hex of hash) -> (version, status, error)."""
        with self.runtime.dbs.index.read() as conn:
            return {
                (r.stage, r.content_hash[:8]): (r.stage_version, r.status, r.error)
                for r in conn.execute(select(stage_runs))
                if r.stage != "core.hash"
            }

    def statuses(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for (stage, _), (_, status, _) in sorted(self.runs().items()):
            result.setdefault(stage, []).append(status)
        return result

    def map(self, relpath: str, type_name: str) -> None:
        with self.runtime.dbs.state.write() as conn:
            store.set_type(conn, FolderRef("main", relpath), type_name, actor=ACTOR)

    def jobs(self, type: str) -> list[queue.Job]:
        with self.runtime.dbs.state.read() as conn:
            return [j for j in queue.list_jobs(conn, limit=10_000) if j.type == type]

    def rebuild(self, modules: list[type]) -> None:
        """Restart with a different set of module classes (same config and data)."""
        config = self.runtime.config
        self.runtime.close()
        self.runtime = build_runtime(
            config,
            entry_points=[FakeEntryPoint(m.name, m) for m in modules],  # type: ignore[attr-defined]
        )
        self.runtime.upgrade_databases()


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


@pytest.fixture
def env(tmp_path: Path, rec: Recorder) -> Iterator[Env]:
    e = Env(tmp_path, [pipeline_module(rec)])
    yield e
    e.runtime.close()


def test_stages_run_in_dependency_order_once_per_content(env: Env, rec: Recorder) -> None:
    env.write("a.txt", "alpha")
    env.write("copy/a.txt", "alpha")  # identical content: processed once
    env.write("b.bin", "\x00\x01binary")
    env.scan()
    assert env.statuses() == {
        "pipe.chunk": ["done"],
        "pipe.extract": ["done"],
        "pipe.names": ["done", "done"],
    }
    assert [c for c in rec.calls if c[0] != "pipe.names"] == [
        ("pipe.extract", "alpha"),
        ("pipe.chunk", "alpha"),
    ]
    assert env.drain() == 0
    assert env.scan() == 1  # just the scan job: nothing changed, nothing re-run


def test_moving_a_file_reruns_nothing(env: Env, rec: Recorder) -> None:
    env.write("a.txt", "alpha")
    env.scan()
    calls = len(rec.calls)
    (env.root / "a.txt").rename(env.root / "moved.txt")
    env.scan()
    assert len(rec.calls) == calls


def test_changed_content_gets_new_results(env: Env, rec: Recorder) -> None:
    env.write("a.txt", "alpha")
    env.scan()
    env.write("a.txt", "beta, longer")
    env.scan()
    assert ("pipe.chunk", "beta, longer") in rec.calls
    assert env.statuses()["pipe.chunk"] == ["done", "done"]  # keyed by content


def test_folder_types_add_and_remove_stages(env: Env, rec: Recorder) -> None:
    with env.runtime.dbs.state.write() as conn:
        store.define_type(
            conn,
            "special",
            TypeDefinition(stages={"add": ["extra"], "remove": ["pipe.chunk"]}),
            actor=ACTOR,
        )
    env.map("special", "special")
    env.write("plain/a.txt", "alpha")
    env.write("special/b.txt", "bravo")
    env.scan()
    called = {(stage, content) for stage, content in rec.calls if stage != "pipe.names"}
    assert called == {
        ("pipe.extract", "alpha"),
        ("pipe.chunk", "alpha"),
        ("pipe.extract", "bravo"),
        ("pipe.extra", "bravo"),
    }


def test_metadata_folders_only_get_metadata_stages(env: Env, rec: Recorder) -> None:
    env.map("old", "archive")  # index: metadata
    env.map("skip", "ignored")  # index: none (not even discovered)
    env.write("old/a.txt", "alpha")
    env.write("skip/b.txt", "bravo")
    env.scan()
    assert rec.calls == [("pipe.names", "alpha")]


def test_changing_a_folder_type_plans_newly_wanted_stages(env: Env, rec: Recorder) -> None:
    env.map("docs", "archive")
    env.write("docs/a.txt", "alpha")
    env.scan()
    assert "pipe.extract" not in env.statuses()
    env.map("docs", "documents")
    env.drain()  # folder.type_changed -> plan job -> full pass -> stage jobs
    assert env.statuses()["pipe.chunk"] == ["done"]


def test_skipped_results_stop_dependents(env: Env, rec: Recorder) -> None:
    rec.behaviour["pipe.extract"] = lambda ctx: StageResult("skipped", reason="no text")
    env.write("a.txt", "alpha")
    env.scan()
    assert env.statuses() == {"pipe.extract": ["skipped"], "pipe.names": ["done"]}
    ((_, _, reason),) = [v for k, v in env.runs().items() if k[0] == "pipe.extract"]
    assert reason == "no text"


def test_failures_are_recorded_retried_and_not_replanned(env: Env, rec: Recorder) -> None:
    def broken(ctx: StageContext) -> StageResult | None:
        raise PermanentJobError("can't parse")

    rec.behaviour["pipe.extract"] = broken
    env.write("a.txt", "alpha")
    env.scan()
    assert env.statuses() == {"pipe.extract": ["failed"], "pipe.names": ["done"]}
    (dead,) = env.jobs(stage_job_type("pipe.extract"))
    assert dead.status == "dead"
    # A full planning pass doesn't requeue a failure at the current version...
    with env.runtime.dbs.state.write() as conn:
        enqueue_full_pass(conn)
    env.drain()
    assert len(env.jobs(stage_job_type("pipe.extract"))) == 1
    # ...but retrying the job does, and its dependents follow.
    del rec.behaviour["pipe.extract"]
    with env.runtime.dbs.state.write() as conn:
        queue.retry(conn, dead.job_id)
    env.drain()
    assert env.statuses()["pipe.chunk"] == ["done"]


def test_a_file_changed_under_a_stage_is_rescanned(env: Env, rec: Recorder) -> None:
    path = env.write("a.txt", "alpha")
    original = path.stat()

    def change_file(ctx: StageContext) -> StageResult | None:
        if not rec.behaviour.get("changed_once"):
            rec.behaviour["changed_once"] = lambda c: None
            path.write_text("changed")
        return None

    rec.behaviour["pipe.extract"] = change_file
    env.scan()
    assert original.st_size != path.stat().st_size
    # The first run saw the file change and was dropped; the rescan found new content.
    assert ("pipe.chunk", "changed") in rec.calls
    assert env.statuses()["pipe.extract"] == ["done"]
    assert any(j.payload.get("paths") == ["a.txt"] for j in env.jobs(SCAN_JOB))


def test_open_refuses_a_file_that_changed_since_the_scan(env: Env, rec: Recorder) -> None:
    env.write("a.txt", "alpha")
    with env.runtime.dbs.state.write() as conn:
        enqueue_full_scan(conn, "main")
    worker = Worker(env.runtime)
    # Scan, but stop before planning (cpu lane) and the stage jobs.
    while worker.run_one("io"):
        worker.housekeeping()
    env.write("a.txt", "different")
    worker.drain()
    # The planned job for "alpha" found the file changed: no result, a rescan instead.
    assert [c for c in rec.calls if c[0] == "pipe.extract"] == [("pipe.extract", "different")]
    assert any(j.payload.get("paths") == ["a.txt"] for j in env.jobs(SCAN_JOB))
    assert env.statuses()["pipe.extract"] == ["done"]


def test_version_bump_reruns_the_stage_and_its_dependents(
    env: Env, rec: Recorder, tmp_path: Path
) -> None:
    env.write("a.txt", "alpha")
    env.scan()
    assert full_pass_needed(env.runtime.dbs, env.runtime.stages)  # never ran one
    with env.runtime.dbs.state.write() as conn:
        enqueue_full_pass(conn)
    env.drain()
    assert not full_pass_needed(env.runtime.dbs, env.runtime.stages)
    rec.calls.clear()

    env.rebuild([pipeline_module(rec, version=2)])
    assert full_pass_needed(env.runtime.dbs, env.runtime.stages)
    with env.runtime.dbs.state.write() as conn:
        enqueue_full_pass(conn)
    env.drain()
    assert rec.calls == [("pipe.extract", "alpha"), ("pipe.chunk", "alpha")]
    assert env.runs()[("pipe.extract", next(iter(env.runs()))[1])][0] == 2


def test_full_pass_is_a_daily_schedule(env: Env) -> None:
    assert str(env.runtime.schedules[FULL_JOB].when) == "daily 04:20"


def test_invalid_graphs_stop_startup(tmp_path: Path, rec: Recorder) -> None:
    def register(r: Registry) -> None:
        r.stage("bad.s", rec.stage("bad.s"), version=1, requires=("nothing",), produces=("x",))

    with pytest.raises(StageGraphError, match="no enabled stage produces"):
        make_runtime(tmp_path, [make_module("bad", register=register)])


def test_two_modules_cannot_register_the_same_stage(tmp_path: Path, rec: Recorder) -> None:
    def register(r: Registry) -> None:
        r.stage("core.sneaky", rec.stage("x"), version=1, produces=("x",))

    with pytest.raises(RegistrationError, match="must start with 'evil.'"):
        make_runtime(tmp_path, [make_module("evil", register=register)])


# --- the hello module (Phase 1 acceptance) -------------------------------------------


def test_hello_module_adds_a_stage_and_subscriber_and_disabling_removes_them(
    tmp_path: Path, rec: Recorder
) -> None:
    e = Env(tmp_path, [HelloModule])
    try:
        e.write("notes/a.md", "one\ntwo\nthree")
        e.write("photo.jpg", "\xff\xd8\xff not really")
        e.scan()
        with e.runtime.dbs.index.read() as conn:
            counts = conn.execute(
                select(stage_runs.c.status).where(stage_runs.c.stage == "hello.lines")
            )
            assert list(counts.scalars()) == ["done"]
            lines = conn.exec_driver_sql("SELECT lines FROM hello_line_counts").scalars().all()
        assert lines == [3]
        assert [j.status for j in e.jobs("core.deliver.hello.greet")] == ["done", "done"]
        assert "hello.lines" in e.runtime.stages.stages

        # Restart with hello installed but disabled.
        write_config(tmp_path / "config", modules={"modules": {"hello": {"enabled": False}}})
        e.runtime.close()
        e.runtime = build_runtime(
            load_config(tmp_path / "config"), entry_points=[FakeEntryPoint("hello", HelloModule)]
        )
        assert "hello.lines" not in e.runtime.stages.stages
        assert stage_job_type("hello.lines") not in e.runtime.registrations.job_types()
        assert "hello.greet" not in e.runtime.registrations.subscriptions()
    finally:
        e.runtime.close()
