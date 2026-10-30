# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Per-file sensitivity overrides (`file_sensitivity`): raise one file above its folder.

Overrides come from you, or from the content scan (actor `rule:secret-scan:<kind>`), which
holds back a file found to contain a credential (docs/adr/0029).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlalchemy import Connection, delete, select
from sqlalchemy.dialects.sqlite import insert

from steward.core import events
from steward.core.db.schema import file_sensitivity, files
from steward.core.db.types import utcnow

Override = Literal["local-only", "excluded"]
SCAN_ACTOR = "rule:secret-scan"


class UnknownFileError(LookupError):
    pass


def held_back(conn: Connection) -> dict[str, str]:
    """Files the content scan holds back: `file_id` → the kind of credential found."""
    rows = conn.execute(
        select(file_sensitivity.c.file_id, file_sensitivity.c.actor).where(
            file_sensitivity.c.actor.like(f"{SCAN_ACTOR}:%")
        )
    )
    return {r.file_id: r.actor.removeprefix(f"{SCAN_ACTOR}:") for r in rows}


def set_override(
    conn: Connection,
    file_id: str,
    level: Override | None,
    *,
    actor: str,
    now: datetime | None = None,
) -> bool:
    """Set (or with None, clear) a file's override. Returns False if nothing changed."""
    now = now or utcnow()
    if conn.execute(select(files.c.file_id).where(files.c.file_id == file_id)).first() is None:
        raise UnknownFileError(file_id)
    current = conn.execute(
        select(file_sensitivity.c.sensitivity).where(file_sensitivity.c.file_id == file_id)
    ).scalar_one_or_none()
    if current == level:
        return False
    if level is None:
        conn.execute(delete(file_sensitivity).where(file_sensitivity.c.file_id == file_id))
    else:
        conn.execute(
            insert(file_sensitivity)
            .values(file_id=file_id, sensitivity=level, actor=actor, updated_at=now)
            .on_conflict_do_update(
                index_elements=[file_sensitivity.c.file_id],
                set_={"sensitivity": level, "actor": actor, "updated_at": now},
            )
        )
    events.emit(
        conn,
        "file.sensitivity_changed",
        {"file_id": file_id, "sensitivity": level, "previous": current},
        actor=actor,
        now=now,
    )
    return True
