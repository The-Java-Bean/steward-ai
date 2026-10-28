# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The scheduler: enqueues registered schedules' jobs when they're due.

State lives in the `schedules` table, so restarts don't lose or repeat runs. Runs
missed while nothing was running collapse into one. A schedule's job uses the dedupe
key `schedule:<name>`, so a slow run is never stacked with another.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, select
from sqlalchemy.dialects.sqlite import insert

from steward.core import queue
from steward.core.db.schema import schedules
from steward.core.db.types import utcnow
from steward.sdk import Daily, Every, JobType, Schedule, When

_EVERY = re.compile(r"^every\s+(\d+)\s*([mhd])$")
_DAILY = re.compile(r"^daily\s+([01]\d|2[0-3]):([0-5]\d)$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_when(text: str) -> When | None:
    """Parse `every 30m` / `every 6h` / `every 1d` / `daily 03:00`; `off` gives None."""
    value = text.strip().lower()
    if value == "off":
        return None
    if m := _EVERY.match(value):
        return Every(timedelta(**{_UNITS[m.group(2)]: int(m.group(1))}))
    if m := _DAILY.match(value):
        return Daily(time(int(m.group(1)), int(m.group(2))))
    raise ValueError(f"schedule {text!r} must be 'every <n>m|h|d', 'daily HH:MM' or 'off'")


def effective_schedules(
    registered: Mapping[str, Schedule], overrides: Mapping[str, str]
) -> dict[str, Schedule]:
    """Apply `pipeline.yaml` overrides; schedules turned `off` are left out."""
    unknown = sorted(set(overrides) - set(registered))
    if unknown:
        raise ValueError(f"pipeline.yaml schedules: unknown schedule(s): {', '.join(unknown)}")
    result = {}
    for name, sched in registered.items():
        when = parse_when(overrides[name]) if name in overrides else sched.when
        if when is not None:
            result[name] = Schedule(when, sched.job, sched.payload)
    return result


def next_run(when: When, after: datetime, tz: ZoneInfo) -> datetime:
    """The first run time strictly after `after` (UTC in, UTC out)."""
    if isinstance(when, Every):
        return after + when.interval
    local = after.astimezone(tz)
    candidate = _at(local.date(), when.at, tz)
    if candidate <= after:
        candidate = _at(local.date() + timedelta(days=1), when.at, tz)
    return candidate


def tick(
    conn: Connection,
    due_schedules: Mapping[str, Schedule],
    job_types: Mapping[str, JobType],
    *,
    tz: ZoneInfo,
    max_attempts: int = 5,
    now: datetime | None = None,
) -> list[str]:
    """Enqueue every due schedule's job. Call inside `Database.write()`. Returns the
    names of schedules that fired."""
    now = now or utcnow()
    state = {r.name: r for r in conn.execute(select(schedules)).all()}
    fired = []
    for name, sched in sorted(due_schedules.items()):
        spec = str(sched.when)
        row = state.get(name)
        if row is None:
            # New schedule: intervals run right away, daily ones at the next slot.
            first = now if isinstance(sched.when, Every) else next_run(sched.when, now, tz)
            _save(conn, name, spec, last_run=None, next_run_at=first, now=now)
            if first > now:
                continue
            next_at = first
        elif row.spec != spec:
            # The timing changed in config: recompute from the last run.
            next_at = next_run(sched.when, row.last_run_at, tz) if row.last_run_at else now
            _save(conn, name, spec, last_run=row.last_run_at, next_run_at=next_at, now=now)
        else:
            next_at = row.next_run_at
        if next_at > now:
            continue
        job_type = job_types[sched.job]
        queue.enqueue(
            conn,
            sched.job,
            lane=job_type.lane,
            payload=sched.payload,
            dedupe_key=f"schedule:{name}",
            max_attempts=max_attempts,
            now=now,
        )
        _save(conn, name, spec, last_run=now, next_run_at=next_run(sched.when, now, tz), now=now)
        fired.append(name)
    return fired


def _at(day: date, at: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, at, tzinfo=tz).astimezone(UTC)


def _save(
    conn: Connection,
    name: str,
    spec: str,
    *,
    last_run: datetime | None,
    next_run_at: datetime,
    now: datetime,
) -> None:
    values = {"spec": spec, "last_run_at": last_run, "next_run_at": next_run_at, "updated_at": now}
    stmt = insert(schedules).values(name=name, **values)
    conn.execute(stmt.on_conflict_do_update(index_elements=[schedules.c.name], set_=values))
