# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The event outbox and its dispatcher.

`emit` writes an event in the caller's transaction, so an event exists exactly when
the change it describes was committed. The dispatcher turns new events into one
delivery job per subscriber (job type `core.deliver.<subscriber>`, dedupe key
`event:<id>:<subscriber>`). Each subscriber has a cursor, so a module that was disabled
picks up where it left off when re-enabled (as long as the events are still retained).
A subscriber seen for the first time starts with events emitted after that moment.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, func, select
from sqlalchemy.dialects.sqlite import insert

from steward.core import queue
from steward.core.db.schema import event_cursors, events
from steward.core.db.types import utcnow
from steward.sdk import Event, Subscription
from steward.sdk.registry import EVENT_TYPE_PATTERN

DELIVERY_PREFIX = "core.deliver."
DISPATCH_BATCH = 500


def delivery_job_type(subscriber: str) -> str:
    return DELIVERY_PREFIX + subscriber


def emit(
    conn: Connection,
    type: str,
    payload: Mapping[str, Any],
    *,
    actor: str,
    version: int = 1,
    now: datetime | None = None,
) -> int:
    """Record an event in the current transaction and return its id.

    Payloads must be JSON-serializable and hold IDs and paths, never file content.
    """
    if not EVENT_TYPE_PATTERN.match(type):
        raise ValueError(f"event type {type!r} must look like 'area.name'")
    json.dumps(payload)  # fail here, not in the dispatcher
    result = conn.execute(
        events.insert().values(
            type=type,
            version=version,
            payload=dict(payload),
            actor=actor,
            occurred_at=now or utcnow(),
        )
    )
    return int(result.inserted_primary_key[0])  # type: ignore[index]


def dispatch(
    conn: Connection,
    subscriptions: Mapping[str, Subscription],
    *,
    max_attempts: int = 5,
    now: datetime | None = None,
) -> int:
    """Enqueue delivery jobs for new events. Call inside `Database.write()`, which holds
    the write lock, so no event can commit behind the cursor. Returns jobs enqueued."""
    now = now or utcnow()
    latest = conn.execute(select(func.coalesce(func.max(events.c.event_id), 0))).scalar_one()
    cursors = dict(
        conn.execute(select(event_cursors.c.subscriber, event_cursors.c.last_event_id)).all()
    )
    enqueued = 0
    for subscriber, sub in sorted(subscriptions.items()):
        if subscriber not in cursors:
            _set_cursor(conn, subscriber, latest, now)
            continue
        rows = (
            conn.execute(
                select(events.c.event_id)
                .where(events.c.type == sub.event_type, events.c.event_id > cursors[subscriber])
                .order_by(events.c.event_id)
                .limit(DISPATCH_BATCH)
            )
            .scalars()
            .all()
        )
        for event_id in rows:
            queue.enqueue(
                conn,
                delivery_job_type(subscriber),
                lane="events",
                payload={"event_id": event_id},
                dedupe_key=f"event:{event_id}:{subscriber}",
                max_attempts=max_attempts,
                now=now,
            )
        enqueued += len(rows)
        new_cursor = rows[-1] if len(rows) == DISPATCH_BATCH else latest
        if new_cursor != cursors[subscriber]:
            _set_cursor(conn, subscriber, new_cursor, now)
    return enqueued


def load(conn: Connection, event_id: int) -> Event | None:
    row = conn.execute(select(events).where(events.c.event_id == event_id)).mappings().one_or_none()
    if row is None:
        return None
    return Event(
        event_id=row["event_id"],
        type=row["type"],
        version=row["version"],
        payload=row["payload"],
        actor=row["actor"],
        occurred_at=row["occurred_at"],
    )


def prune(conn: Connection, *, older_than: datetime) -> int:
    """Delete events older than the retention period."""
    return conn.execute(events.delete().where(events.c.occurred_at < older_than)).rowcount


def _set_cursor(conn: Connection, subscriber: str, last_event_id: int, now: datetime) -> None:
    stmt = insert(event_cursors).values(
        subscriber=subscriber, last_event_id=last_event_id, updated_at=now
    )
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[event_cursors.c.subscriber],
            set_={"last_event_id": last_event_id, "updated_at": now},
        )
    )
