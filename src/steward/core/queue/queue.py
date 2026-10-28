# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The job queue: a `jobs` table in state.db with leases instead of heartbeats.

Lifecycle: `queued` -> `running` (claimed with a lease) -> `done`, or back to `queued`
with backoff after a failure, or `dead` after `max_attempts`. A worker that crashes
simply stops renewing; when its lease expires another worker takes the job over, and
the crashed worker's late `complete` or `fail` is ignored, so a job is never finished
twice. Handlers must still be idempotent, because the work itself may run twice.

Every function takes a connection from `Database.write()` (or `read()` for queries),
so callers can enqueue in the same transaction as the change that caused the job.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, RowMapping, and_, case, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert

from steward.core.db.schema import ACTIVE_JOB_STATUSES, jobs
from steward.core.db.types import utcnow
from steward.sdk import Lane

MAX_ERROR_CHARS = 2000
LEASE_EXPIRED = "lease expired: the worker stopped or took too long"


@dataclass(frozen=True, slots=True)
class Job:
    job_id: int
    type: str
    lane: Lane
    payload: Mapping[str, Any]
    priority: int
    status: str
    attempts: int
    max_attempts: int
    run_after: datetime
    lease_until: datetime | None
    worker_id: str | None
    last_error: str | None
    dedupe_key: str | None
    created_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class Backoff:
    base_seconds: int = 30
    max_seconds: int = 3600

    def delay(self, attempts: int) -> timedelta:
        """Delay before the next try after `attempts` failed attempts (1, 2, ...)."""
        seconds = self.base_seconds * 2 ** max(attempts - 1, 0)
        return timedelta(seconds=min(seconds, self.max_seconds))


def enqueue(
    conn: Connection,
    type: str,
    *,
    lane: Lane,
    payload: Mapping[str, Any] | None = None,
    priority: int = 0,
    run_after: datetime | None = None,
    dedupe_key: str | None = None,
    max_attempts: int = 5,
    now: datetime | None = None,
) -> int:
    """Add a job and return its id.

    If a queued or running job with the same `dedupe_key` exists, nothing is added and
    that job's id is returned.
    """
    now = now or utcnow()
    stmt = (
        insert(jobs)
        .values(
            type=type,
            lane=lane,
            payload=dict(payload or {}),
            priority=priority,
            status="queued",
            attempts=0,
            max_attempts=max_attempts,
            run_after=run_after or now,
            dedupe_key=dedupe_key,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing()
        .returning(jobs.c.job_id)
    )
    job_id = conn.execute(stmt).scalar_one_or_none()
    if job_id is not None:
        return int(job_id)
    existing = conn.execute(
        select(jobs.c.job_id).where(
            jobs.c.dedupe_key == dedupe_key, jobs.c.status.in_(ACTIVE_JOB_STATUSES)
        )
    ).scalar_one()
    return int(existing)


def claim(
    conn: Connection,
    *,
    lane: Lane,
    types: Collection[str],
    worker_id: str,
    lease: timedelta,
    now: datetime | None = None,
) -> Job | None:
    """Reserve the next runnable job on `lane` whose type is in `types`.

    Runnable means queued and due, or running with an expired lease (its worker is
    gone). Highest priority first, then oldest. Jobs of other types stay queued.
    """
    if not types:
        return None
    now = now or utcnow()
    _bury_expired(conn, lane=lane, now=now)

    expired = and_(jobs.c.status == "running", jobs.c.lease_until < now)
    next_job = (
        select(jobs.c.job_id)
        .where(
            jobs.c.lane == lane,
            jobs.c.type.in_(list(types)),
            or_(and_(jobs.c.status == "queued", jobs.c.run_after <= now), expired),
        )
        .order_by(jobs.c.priority.desc(), jobs.c.run_after, jobs.c.job_id)
        .limit(1)
        .scalar_subquery()
    )
    row = (
        conn.execute(
            update(jobs)
            .where(jobs.c.job_id == next_job)
            .values(
                status="running",
                attempts=jobs.c.attempts + 1,
                worker_id=worker_id,
                lease_until=now + lease,
                updated_at=now,
                last_error=case(
                    (jobs.c.status == "running", LEASE_EXPIRED), else_=jobs.c.last_error
                ),
            )
            .returning(*jobs.c)
        )
        .mappings()
        .one_or_none()
    )
    return _job(row) if row else None


def renew(
    conn: Connection, job_id: int, *, worker_id: str, lease: timedelta, now: datetime | None = None
) -> bool:
    """Extend a running job's lease. False if this worker no longer holds it."""
    now = now or utcnow()
    result = conn.execute(
        update(jobs)
        .where(*_held_by(job_id, worker_id))
        .values(lease_until=now + lease, updated_at=now)
    )
    return result.rowcount == 1


def complete(conn: Connection, job_id: int, *, worker_id: str, now: datetime | None = None) -> bool:
    """Mark a job done. False (and no change) if this worker no longer holds it."""
    now = now or utcnow()
    result = conn.execute(
        update(jobs)
        .where(*_held_by(job_id, worker_id))
        .values(status="done", lease_until=None, finished_at=now, updated_at=now)
    )
    return result.rowcount == 1


def fail(
    conn: Connection,
    job_id: int,
    *,
    worker_id: str,
    error: str,
    backoff: Backoff,
    permanent: bool = False,
    now: datetime | None = None,
) -> str | None:
    """Record a failed attempt. Returns the new status (`queued` or `dead`), or None if
    this worker no longer holds the job."""
    now = now or utcnow()
    row = conn.execute(
        select(jobs.c.attempts, jobs.c.max_attempts).where(*_held_by(job_id, worker_id))
    ).one_or_none()
    if row is None:
        return None
    attempts, max_attempts = row
    dead = permanent or attempts >= max_attempts
    values: dict[str, Any] = {
        "last_error": error[:MAX_ERROR_CHARS],
        "lease_until": None,
        "updated_at": now,
    }
    if dead:
        values.update(status="dead", finished_at=now)
    else:
        values.update(status="queued", run_after=now + backoff.delay(attempts))
    conn.execute(update(jobs).where(*_held_by(job_id, worker_id)).values(**values))
    return "dead" if dead else "queued"


def retry(conn: Connection, job_id: int, *, now: datetime | None = None) -> bool:
    """Put a dead job back in the queue with a fresh set of attempts."""
    now = now or utcnow()
    result = conn.execute(
        update(jobs)
        .where(jobs.c.job_id == job_id, jobs.c.status == "dead")
        .values(
            status="queued",
            attempts=0,
            run_after=now,
            finished_at=None,
            worker_id=None,
            updated_at=now,
        )
    )
    return result.rowcount == 1


def prune_done(conn: Connection, *, older_than: datetime) -> int:
    """Delete finished (`done`) jobs older than `older_than`. Dead jobs are kept."""
    result = conn.execute(
        jobs.delete().where(jobs.c.status == "done", jobs.c.finished_at < older_than)
    )
    return result.rowcount


def get(conn: Connection, job_id: int) -> Job | None:
    row = conn.execute(select(jobs).where(jobs.c.job_id == job_id)).mappings().one_or_none()
    return _job(row) if row else None


def list_jobs(conn: Connection, *, status: str | None = None, limit: int = 50) -> list[Job]:
    stmt = select(jobs).order_by(jobs.c.job_id.desc()).limit(limit)
    if status:
        stmt = stmt.where(jobs.c.status == status)
    return [_job(r) for r in conn.execute(stmt).mappings()]


def counts(conn: Connection) -> dict[tuple[str, str], int]:
    """(lane, status) -> number of jobs."""
    rows = conn.execute(
        select(jobs.c.lane, jobs.c.status, func.count()).group_by(jobs.c.lane, jobs.c.status)
    )
    return {(lane, status): n for lane, status, n in rows}


def _bury_expired(conn: Connection, *, lane: Lane, now: datetime) -> None:
    """Jobs whose lease expired on their last allowed attempt go to `dead`."""
    conn.execute(
        update(jobs)
        .where(
            jobs.c.lane == lane,
            jobs.c.status == "running",
            jobs.c.lease_until < now,
            jobs.c.attempts >= jobs.c.max_attempts,
        )
        .values(
            status="dead",
            last_error=LEASE_EXPIRED,
            lease_until=None,
            finished_at=now,
            updated_at=now,
        )
    )


def _held_by(job_id: int, worker_id: str) -> tuple[Any, ...]:
    return (jobs.c.job_id == job_id, jobs.c.status == "running", jobs.c.worker_id == worker_id)


def _job(row: RowMapping) -> Job:
    return Job(**{f.name: row[f.name] for f in fields(Job)})
