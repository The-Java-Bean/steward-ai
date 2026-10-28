# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from support import make_runtime

from steward.core import queue
from steward.core.db import Database

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
LEASE = timedelta(minutes=5)
BACKOFF = queue.Backoff(base_seconds=30, max_seconds=3600)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return make_runtime(tmp_path).dbs.state


def add(db: Database, type: str = "a.work", **kwargs: object) -> int:
    kwargs.setdefault("lane", "cpu")
    kwargs.setdefault("now", T0)
    with db.write() as conn:
        return queue.enqueue(conn, type, **kwargs)  # type: ignore[arg-type]


def take(
    db: Database, *, worker: str = "w1", now: datetime = T0, types: tuple[str, ...] = ("a.work",)
) -> queue.Job | None:
    with db.write() as conn:
        return queue.claim(conn, lane="cpu", types=types, worker_id=worker, lease=LEASE, now=now)


def get(db: Database, job_id: int) -> queue.Job:
    with db.read() as conn:
        job = queue.get(conn, job_id)
    assert job is not None
    return job


def test_enqueue_claim_complete(db: Database) -> None:
    job_id = add(db, payload={"path": "a/b.txt"})
    job = take(db)
    assert job is not None
    assert (job.job_id, job.status, job.attempts, job.payload) == (
        job_id,
        "running",
        1,
        {"path": "a/b.txt"},
    )
    assert job.lease_until == T0 + LEASE
    with db.write() as conn:
        assert queue.complete(conn, job_id, worker_id="w1", now=T0)
    assert get(db, job_id).status == "done"
    assert take(db) is None


def test_priority_then_age(db: Database) -> None:
    low = add(db, now=T0)
    high = add(db, priority=5, now=T0 + timedelta(seconds=1))
    older = add(db, now=T0 - timedelta(seconds=1))
    later = T0 + timedelta(seconds=2)
    assert [take(db, now=later).job_id for _ in range(3)] == [high, older, low]  # type: ignore[union-attr]


def test_run_after_is_respected(db: Database) -> None:
    add(db, run_after=T0 + timedelta(minutes=10))
    assert take(db, now=T0) is None
    assert take(db, now=T0 + timedelta(minutes=10)) is not None


def test_only_requested_lanes_and_types_are_claimed(db: Database) -> None:
    add(db, "a.other")
    add(db, lane="io")
    assert take(db) is None
    assert take(db, types=("a.other",)) is not None
    assert take(db, types=()) is None


def test_dedupe_key_returns_the_active_job(db: Database) -> None:
    first = add(db, dedupe_key="scan:1")
    assert add(db, dedupe_key="scan:1") == first
    take(db)
    assert add(db, dedupe_key="scan:1") == first  # still running
    with db.write() as conn:
        queue.complete(conn, first, worker_id="w1")
    assert add(db, dedupe_key="scan:1") != first  # finished jobs don't block new ones


def test_failure_retries_with_exponential_backoff_then_dies(db: Database) -> None:
    job_id = add(db, max_attempts=3)
    now = T0
    for attempt, delay in ((1, 30), (2, 60)):
        assert take(db, now=now).attempts == attempt  # type: ignore[union-attr]
        with db.write() as conn:
            status = queue.fail(
                conn, job_id, worker_id="w1", error="boom", backoff=BACKOFF, now=now
            )
        assert status == "queued"
        assert get(db, job_id).run_after == now + timedelta(seconds=delay)
        assert take(db, now=now + timedelta(seconds=delay - 1)) is None
        now += timedelta(seconds=delay)
    take(db, now=now)
    with db.write() as conn:
        assert (
            queue.fail(conn, job_id, worker_id="w1", error="boom", backoff=BACKOFF, now=now)
            == "dead"
        )
    job = get(db, job_id)
    assert (job.status, job.last_error, job.attempts) == ("dead", "boom", 3)


def test_permanent_failure_dies_immediately(db: Database) -> None:
    job_id = add(db)
    take(db)
    with db.write() as conn:
        status = queue.fail(
            conn, job_id, worker_id="w1", error="bad", backoff=BACKOFF, permanent=True
        )
    assert status == "dead"


def test_backoff_is_capped() -> None:
    assert BACKOFF.delay(20) == timedelta(hours=1)


def test_crashed_worker_job_is_taken_over_without_double_completion(db: Database) -> None:
    job_id = add(db)
    assert take(db, worker="crashed") is not None
    # Lease still valid: nobody else may take it.
    assert take(db, worker="w2", now=T0 + LEASE - timedelta(seconds=1)) is None
    # Lease expired: the job is handed over, and the error says why.
    later = T0 + LEASE + timedelta(seconds=1)
    job = take(db, worker="w2", now=later)
    assert job is not None
    assert (job.job_id, job.attempts, job.worker_id) == (job_id, 2, "w2")
    assert "lease expired" in (job.last_error or "")
    # The crashed worker comes back late: its completion and failure are ignored.
    with db.write() as conn:
        assert not queue.complete(conn, job_id, worker_id="crashed")
        assert queue.fail(conn, job_id, worker_id="crashed", error="x", backoff=BACKOFF) is None
        assert not queue.renew(conn, job_id, worker_id="crashed", lease=LEASE)
        assert queue.complete(conn, job_id, worker_id="w2")
    assert get(db, job_id).status == "done"


def test_expired_lease_on_last_attempt_goes_dead(db: Database) -> None:
    job_id = add(db, max_attempts=1)
    take(db, worker="crashed")
    assert take(db, worker="w2", now=T0 + LEASE * 2) is None
    job = get(db, job_id)
    assert job.status == "dead"
    assert "lease expired" in (job.last_error or "")


def test_renew_extends_the_lease(db: Database) -> None:
    job_id = add(db)
    take(db)
    with db.write() as conn:
        assert queue.renew(conn, job_id, worker_id="w1", lease=LEASE, now=T0 + timedelta(minutes=4))
    assert take(db, worker="w2", now=T0 + timedelta(minutes=6)) is None


def test_retry_dead_job(db: Database) -> None:
    job_id = add(db, max_attempts=1)
    take(db)
    with db.write() as conn:
        queue.fail(conn, job_id, worker_id="w1", error="x", backoff=BACKOFF)
        assert queue.retry(conn, job_id, now=T0)
        assert not queue.retry(conn, job_id)  # only dead jobs
    assert take(db) is not None


def test_prune_done_keeps_dead_and_recent(db: Database) -> None:
    old_done, recent_done, dead = add(db), add(db), add(db, max_attempts=1)
    with db.write() as conn:
        for job_id, when in ((old_done, T0), (recent_done, T0 + timedelta(days=10))):
            queue.claim(conn, lane="cpu", types=["a.work"], worker_id="w", lease=LEASE, now=when)
            queue.complete(conn, job_id, worker_id="w", now=when)
        queue.claim(conn, lane="cpu", types=["a.work"], worker_id="w", lease=LEASE, now=T0)
        queue.fail(conn, dead, worker_id="w", error="x", backoff=BACKOFF, now=T0)
        assert queue.prune_done(conn, older_than=T0 + timedelta(days=5)) == 1
        assert {(lane, status) for lane, status in queue.counts(conn)} == {
            ("cpu", "done"),
            ("cpu", "dead"),
        }


def test_long_errors_are_truncated(db: Database) -> None:
    job_id = add(db)
    take(db)
    with db.write() as conn:
        queue.fail(conn, job_id, worker_id="w1", error="x" * 10_000, backoff=BACKOFF)
    assert len(get(db, job_id).last_error or "") == queue.queue.MAX_ERROR_CHARS
