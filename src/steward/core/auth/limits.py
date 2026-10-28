# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Rate limiting of password checks, backed by `login_attempts` so restarts don't reset it.

Two keys are counted: the client address (any username) and the username (any
address). A key is locked out when it has at least its maximum of failures within the
window and recorded after its last success, until `lockout_minutes` after the latest failure.
While locked out, attempts are refused without checking the password and aren't
recorded, so an attacker can't extend a lockout forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import Column, Connection, delete, func, select

from steward.core.config import LoginLimits
from steward.core.db.schema import login_attempts

type LimitScope = Literal["ip", "account"]
type AttemptKind = Literal["login", "reauth"]


@dataclass(frozen=True)
class Lockout:
    scope: LimitScope
    until: datetime

    def retry_after(self, now: datetime) -> int:
        return max(1, int((self.until - now).total_seconds()) + 1)


def check(
    conn: Connection, limits: LoginLimits, *, username: str, ip: str, now: datetime
) -> Lockout | None:
    """The lockout that applies right now, the later-ending one if both do."""
    found = [
        lockout
        for lockout in (
            _check_key(conn, limits, login_attempts.c.ip, ip, limits.max_failures_per_ip, now),
            _check_key(
                conn,
                limits,
                login_attempts.c.username,
                username,
                limits.max_failures_per_account,
                now,
            ),
        )
        if lockout is not None
    ]
    return max(found, key=lambda lockout: lockout.until) if found else None


def _check_key(
    conn: Connection,
    limits: LoginLimits,
    column: Column[str],
    value: str,
    maximum: int,
    now: datetime,
) -> Lockout | None:
    since = now - timedelta(minutes=limits.window_minutes)
    last_success = conn.execute(
        select(func.max(login_attempts.c.attempt_id)).where(
            column == value, login_attempts.c.success
        )
    ).scalar()
    failures = conn.execute(
        select(func.count(), func.max(login_attempts.c.at)).where(
            column == value,
            ~login_attempts.c.success,
            login_attempts.c.at > since,
            login_attempts.c.attempt_id > (last_success or 0),
        )
    ).one()
    count, latest = failures
    if count < maximum or latest is None:
        return None
    until = latest + timedelta(minutes=limits.lockout_minutes)
    if now >= until:
        return None
    return Lockout("ip" if column is login_attempts.c.ip else "account", until)


def record(
    conn: Connection,
    *,
    username: str,
    ip: str,
    kind: AttemptKind,
    success: bool,
    now: datetime,
) -> None:
    conn.execute(
        login_attempts.insert().values(username=username, ip=ip, kind=kind, success=success, at=now)
    )


def clear(conn: Connection, *, username: str | None = None, ip: str | None = None) -> int:
    """Forget failures for a username and/or address (`steward user unlock`)."""
    conditions = []
    if username is not None:
        conditions.append(login_attempts.c.username == username)
    if ip is not None:
        conditions.append(login_attempts.c.ip == ip)
    if not conditions:
        raise ValueError("give a username or an address")
    stmt = delete(login_attempts).where(~login_attempts.c.success, *conditions)
    return conn.execute(stmt).rowcount


def failures_since(conn: Connection, since: datetime) -> list[tuple[str, str, str, datetime]]:
    """(username, ip, kind, at) of recent failures, newest first, for the health page."""
    rows = conn.execute(
        select(
            login_attempts.c.username,
            login_attempts.c.ip,
            login_attempts.c.kind,
            login_attempts.c.at,
        )
        .where(~login_attempts.c.success, login_attempts.c.at >= since)
        .order_by(login_attempts.c.at.desc())
    )
    return [(r.username, r.ip, r.kind, r.at) for r in rows]


def prune(conn: Connection, *, older_than: datetime) -> int:
    return conn.execute(delete(login_attempts).where(login_attempts.c.at < older_than)).rowcount
