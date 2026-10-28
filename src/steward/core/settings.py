# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Runtime settings the owner edits in the UI, stored in `settings_kv` (state.db).

Keys are `<owner>.<name>` (`auth.reauth_minutes`). A missing key means the default,
which the code that owns the key defines. Values are JSON. Changing a value emits
`settings.changed` in the same transaction; setting the current value again is a no-op.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, delete, select
from sqlalchemy.dialects.sqlite import insert

from steward.core import events
from steward.core.db.schema import settings_kv
from steward.core.db.types import utcnow

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


def check_key(key: str) -> str:
    if not KEY_PATTERN.match(key):
        raise ValueError(f"setting key {key!r} must look like 'owner.name'")
    return key


def get_all(conn: Connection, prefix: str) -> dict[str, Any]:
    """Stored values whose key starts with `<prefix>.`, keyed by the rest of the key."""
    start = check_key(f"{prefix}.x")[:-1]
    rows = conn.execute(
        select(settings_kv.c.key, settings_kv.c.value).where(settings_kv.c.key.startswith(start))
    )
    return {r.key[len(start) :]: r.value for r in rows}


def set_value(
    conn: Connection, key: str, value: Any, *, actor: str, now: datetime | None = None
) -> bool:
    """Store `value` under `key`. Returns False (and emits nothing) if it was already set."""
    check_key(key)
    json.dumps(value)
    current = conn.execute(
        select(settings_kv.c.value).where(settings_kv.c.key == key)
    ).one_or_none()
    if current is not None and current.value == value:
        return False
    now = now or utcnow()
    stmt = insert(settings_kv).values(key=key, value=value, actor=actor, updated_at=now)
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[settings_kv.c.key],
            set_={"value": stmt.excluded.value, "actor": actor, "updated_at": now},
        )
    )
    events.emit(conn, "settings.changed", {"key": key, "value": value}, actor=actor, now=now)
    return True


def clear(conn: Connection, key: str, *, actor: str, now: datetime | None = None) -> bool:
    """Go back to the default. Returns False if nothing was stored."""
    check_key(key)
    if conn.execute(delete(settings_kv).where(settings_kv.c.key == key)).rowcount == 0:
        return False
    events.emit(conn, "settings.changed", {"key": key, "value": None}, actor=actor, now=now)
    return True
