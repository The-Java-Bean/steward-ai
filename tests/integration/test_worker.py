# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

import threading
import time as time_module
from datetime import timedelta
from pathlib import Path
from typing import Any

from support import make_module, make_runtime

from steward.core import events, queue
from steward.core.db import utcnow
from steward.core.runtime import Runtime
from steward.core.worker import Worker
from steward.sdk import Event, Every, JobContext, PermanentJobError, Registry


class Recorder:
    """A throwaway module's handlers, recording what ran."""

    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []
        self.events: list[Event] = []
        self.fail_times = 0

    def register(self, r: Registry) -> None:
        r.job("notes.work", self.work, lane="cpu")
        r.job("notes.flaky", self.flaky, lane="cpu")
        r.job("notes.broken", self.broken, lane="io")
        r.subscribe("file.discovered", self.events.append, name="notes.on_file")
        r.schedule(
            "notes.hourly",
            Every(timedelta(hours=1)),
            job="notes.work",
            payload={"from": "schedule"},
        )

    def work(self, ctx: JobContext) -> None:
        self.jobs.append(dict(ctx.payload))

    def flaky(self, ctx: JobContext) -> None:
        if ctx.attempt <= self.fail_times:
            raise RuntimeError("temporary problem")
        self.jobs.append({"flaky_attempt": ctx.attempt})

    def broken(self, ctx: JobContext) -> None:
        raise PermanentJobError("can't ever work")


def setup(tmp_path: Path, pipeline: dict[str, Any] | None = None) -> tuple[Runtime, Recorder]:
    rec = Recorder()
    module = make_module("notes", register=rec.register)
    runtime = make_runtime(tmp_path, [module], pipeline=pipeline or {})
    return runtime, rec


def enqueue(runtime: Runtime, type: str, lane: str = "cpu", **payload: Any) -> int:
    with runtime.dbs.state.write() as conn:
        return queue.enqueue(conn, type, lane=lane, payload=payload)  # type: ignore[arg-type]


def job(runtime: Runtime, job_id: int) -> queue.Job:
    with runtime.dbs.state.read() as conn:
        found = queue.get(conn, job_id)
    assert found is not None
    return found


def test_jobs_events_and_schedules_flow_through(tmp_path: Path) -> None:
    runtime, rec = setup(tmp_path, pipeline={"schedules": {"core.reconcile": "off"}})
    worker = Worker(runtime)
    worker.housekeeping()  # registers the subscriber's cursor; fires the new schedule
    with runtime.dbs.state.write() as conn:
        event_id = events.emit(conn, "file.discovered", {"file_id": 7}, actor="system")
    enqueue(runtime, "notes.work", n=1)

    # the schedule's job, notes.work, the delivery, and core.stages.plan (a file event)
    assert worker.drain() == 4
    assert sorted(map(str, rec.jobs)) == ["{'from': 'schedule'}", "{'n': 1}"]
    assert [(e.event_id, e.payload) for e in rec.events] == [(event_id, {"file_id": 7})]
    assert worker.drain() == 0  # nothing delivered or run twice


def test_failures_retry_with_backoff(tmp_path: Path) -> None:
    runtime, rec = setup(tmp_path, pipeline={"jobs": {"backoff_base_seconds": 1}})
    rec.fail_times = 1
    worker = Worker(runtime)
    job_id = enqueue(runtime, "notes.flaky")
    assert worker.run_one("cpu")
    failed = job(runtime, job_id)
    assert (failed.status, failed.last_error) == ("queued", "RuntimeError: temporary problem")
    assert not worker.run_one("cpu")  # backing off
    assert worker.run_one("cpu", now=utcnow() + timedelta(seconds=2))
    assert job(runtime, job_id).status == "done"
    assert rec.jobs == [{"flaky_attempt": 2}]


def test_permanent_errors_go_dead(tmp_path: Path) -> None:
    runtime, _ = setup(tmp_path)
    job_id = enqueue(runtime, "notes.broken", lane="io")
    Worker(runtime).drain()
    assert job(runtime, job_id).status == "dead"


def test_jobs_of_a_disabled_module_wait(tmp_path: Path) -> None:
    runtime, _ = setup(tmp_path)
    Worker(runtime).housekeeping()
    with runtime.dbs.state.write() as conn:
        events.emit(conn, "file.discovered", {"file_id": 1}, actor="system")
    Worker(runtime).housekeeping()  # delivery job queued
    job_id = enqueue(runtime, "notes.work")
    runtime.close()

    data_dir = str(tmp_path / "data")
    without = make_runtime(tmp_path / "off", steward={"data_dir": data_dir})
    Worker(without).drain()
    assert job(without, job_id).status == "queued"

    rec2 = Recorder()
    back = make_runtime(
        tmp_path / "on",
        [make_module("notes", register=rec2.register)],
        steward={"data_dir": data_dir},
    )
    Worker(back).drain()
    assert job(back, job_id).status == "done"
    assert [e.payload for e in rec2.events] == [{"file_id": 1}]


def test_a_crashed_workers_job_is_finished_by_another(tmp_path: Path) -> None:
    runtime, rec = setup(tmp_path)
    job_id = enqueue(runtime, "notes.work", n=1)
    with runtime.dbs.state.write() as conn:  # a worker claims it, then dies
        queue.claim(
            conn,
            lane="cpu",
            types=["notes.work"],
            worker_id="dead-worker",
            lease=timedelta(minutes=5),
        )
    survivor = Worker(runtime)
    assert not survivor.run_one("cpu")
    assert survivor.run_one("cpu", now=utcnow() + timedelta(minutes=6))
    assert rec.jobs == [{"n": 1}]
    with runtime.dbs.state.write() as conn:
        assert not queue.complete(conn, job_id, worker_id="dead-worker")
    assert job(runtime, job_id).status == "done"


def test_threaded_run_and_graceful_stop(tmp_path: Path) -> None:
    runtime, rec = setup(
        tmp_path, pipeline={"jobs": {"poll_seconds": 0.02}, "schedules": {"notes.hourly": "off"}}
    )
    ids = [enqueue(runtime, "notes.work", n=i) for i in range(20)]
    stop = threading.Event()
    thread = threading.Thread(target=Worker(runtime).run, args=(stop,), kwargs={"grace_seconds": 5})
    thread.start()
    deadline = time_module.monotonic() + 10
    while len(rec.jobs) < 20 and time_module.monotonic() < deadline:
        time_module.sleep(0.02)
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert sorted(j["n"] for j in rec.jobs) == list(range(20))
    assert {job(runtime, i).status for i in ids} == {"done"}


def test_core_maintenance_jobs_run(tmp_path: Path) -> None:
    runtime, _ = setup(tmp_path)
    old = utcnow() - timedelta(days=400)
    with runtime.dbs.state.write() as conn:
        events.emit(conn, "file.discovered", {}, actor="system", now=old)
    enqueue(runtime, "core.prune_events", lane="io")
    Worker(runtime).drain()
    with runtime.dbs.state.read() as conn:
        assert events.load(conn, 1) is None
