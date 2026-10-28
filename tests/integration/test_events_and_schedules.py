# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from support import make_runtime

from steward.core import events, queue, scheduler
from steward.core.db import Database
from steward.core.db.schema import events as events_table
from steward.core.db.schema import jobs
from steward.sdk import Daily, Every, JobContext, JobType, Schedule, Subscription

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
UTC_TZ = ZoneInfo("UTC")


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return make_runtime(tmp_path).dbs.state


def sub(event_type: str = "file.discovered") -> Subscription:
    return Subscription(event_type, lambda e: None)


def emit(db: Database, type: str = "file.discovered", **payload: object) -> int:
    with db.write() as conn:
        return events.emit(conn, type, payload, actor="system", now=T0)


def dispatch(db: Database, subs: dict[str, Subscription]) -> int:
    with db.write() as conn:
        return events.dispatch(conn, subs, now=T0)


def delivery_jobs(db: Database) -> list[tuple[str, dict[str, object], str | None]]:
    with db.read() as conn:
        rows = conn.execute(
            select(jobs.c.type, jobs.c.payload, jobs.c.dedupe_key).order_by(jobs.c.job_id)
        )
        return [(r.type, r.payload, r.dedupe_key) for r in rows]


# --- events ----------------------------------------------------------------------


def test_emit_is_part_of_the_callers_transaction(db: Database) -> None:
    def failing_change() -> None:
        with db.write() as conn:
            events.emit(conn, "file.discovered", {"file_id": 1}, actor="system")
            raise RuntimeError("the change failed")

    with pytest.raises(RuntimeError):
        failing_change()
    with db.read() as conn:
        assert conn.execute(select(func.count()).select_from(events_table)).scalar() == 0


def test_emit_validates(db: Database) -> None:
    with db.write() as conn:
        with pytest.raises(ValueError, match="area.name"):
            events.emit(conn, "discovered", {}, actor="system")
        with pytest.raises(TypeError):
            events.emit(conn, "file.discovered", {"x": object()}, actor="system")


def test_new_subscriber_starts_from_now(db: Database) -> None:
    emit(db, file_id=1)
    assert dispatch(db, {"a.on_file": sub()}) == 0
    second = emit(db, file_id=2)
    assert dispatch(db, {"a.on_file": sub()}) == 1
    assert delivery_jobs(db) == [
        ("core.deliver.a.on_file", {"event_id": second}, f"event:{second}:a.on_file")
    ]


def test_one_job_per_matching_subscriber_and_no_redelivery(db: Database) -> None:
    subs = {"a.on_file": sub(), "b.on_file": sub(), "c.on_tag": sub("tag.applied")}
    dispatch(db, subs)
    event_id = emit(db, file_id=1)
    assert dispatch(db, subs) == 2
    assert dispatch(db, subs) == 0
    assert sorted(t for t, _, _ in delivery_jobs(db)) == [
        "core.deliver.a.on_file",
        "core.deliver.b.on_file",
    ]
    with db.read() as conn:
        loaded = events.load(conn, event_id)
    assert loaded is not None
    assert (loaded.type, loaded.payload, loaded.actor) == (
        "file.discovered",
        {"file_id": 1},
        "system",
    )


def test_disabled_subscriber_resumes_where_it_left_off(db: Database) -> None:
    both = {"a.on_file": sub(), "b.on_file": sub()}
    dispatch(db, both)
    missed = [emit(db, n=1), emit(db, n=2)]
    dispatch(db, {"a.on_file": sub()})  # module b disabled meanwhile
    dispatch(db, both)  # b re-enabled
    b_jobs = [p["event_id"] for t, p, _ in delivery_jobs(db) if t == "core.deliver.b.on_file"]
    assert b_jobs == missed


def test_prune_removes_old_events(db: Database) -> None:
    emit(db)
    with db.write() as conn:
        assert events.prune(conn, older_than=T0 + timedelta(days=1)) == 1


# --- schedules -------------------------------------------------------------------


def _noop(ctx: JobContext) -> None:
    pass


JOB_TYPES = {"a.job": JobType(_noop, "io")}


def tick(db: Database, schedules: dict[str, Schedule], now: datetime) -> list[str]:
    with db.write() as conn:
        return scheduler.tick(conn, schedules, JOB_TYPES, tz=UTC_TZ, now=now)


def complete_all(db: Database) -> None:
    with db.write() as conn:
        while job := queue.claim(
            conn, lane="io", types=["a.job"], worker_id="w", lease=timedelta(minutes=5)
        ):
            queue.complete(conn, job.job_id, worker_id="w")


def test_interval_schedule_runs_immediately_then_every_interval(db: Database) -> None:
    hourly = {"a.hourly": Schedule(Every(timedelta(hours=1)), "a.job", {"x": 1})}
    assert tick(db, hourly, T0) == ["a.hourly"]
    complete_all(db)
    assert tick(db, hourly, T0 + timedelta(minutes=59)) == []
    assert tick(db, hourly, T0 + timedelta(hours=1)) == ["a.hourly"]
    assert delivery_jobs(db)[0] == ("a.job", {"x": 1}, "schedule:a.hourly")


def test_daily_schedule_waits_for_its_time(db: Database) -> None:
    daily = {"a.daily": Schedule(Daily(time(13, 0)), "a.job", {})}
    assert tick(db, daily, T0) == []
    assert tick(db, daily, T0 + timedelta(minutes=59)) == []
    assert tick(db, daily, T0 + timedelta(hours=1)) == ["a.daily"]
    assert tick(db, daily, T0 + timedelta(hours=2)) == []


def test_missed_runs_collapse_into_one(db: Database) -> None:
    hourly = {"a.hourly": Schedule(Every(timedelta(hours=1)), "a.job", {})}
    tick(db, hourly, T0)
    complete_all(db)
    assert tick(db, hourly, T0 + timedelta(days=3)) == ["a.hourly"]
    assert tick(db, hourly, T0 + timedelta(days=3, minutes=1)) == []


def test_a_slow_run_is_not_stacked(db: Database) -> None:
    every = {"a.often": Schedule(Every(timedelta(minutes=1)), "a.job", {})}
    for minute in range(5):
        tick(db, every, T0 + timedelta(minutes=minute))
    assert len(delivery_jobs(db)) == 1  # still queued, so deduplicated


def test_changed_timing_takes_effect(db: Database) -> None:
    tick(db, {"a.s": Schedule(Every(timedelta(hours=6)), "a.job", {})}, T0)
    complete_all(db)
    shorter = {"a.s": Schedule(Every(timedelta(hours=1)), "a.job", {})}
    assert tick(db, shorter, T0 + timedelta(minutes=30)) == []
    assert tick(db, shorter, T0 + timedelta(hours=1)) == ["a.s"]
