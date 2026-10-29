# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The `shares_*` tables and the queries on them."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Connection,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    Text,
    func,
    insert,
    select,
    update,
)

from steward.sdk import StateAccess, UTCDateTime

metadata = MetaData()

links = Table(
    "shares_links",
    metadata,
    Column("share_id", Text, primary_key=True),
    Column("token", Text, nullable=False, unique=True),
    Column("path", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("password_hash", Text),
    Column("expires_at", UTCDateTime),
    Column("snapshot", Boolean, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    Column("ended_at", UTCDateTime),
    Column("end_reason", Text),
    Column("ended_by", Text),
    Column("access_count", Integer, nullable=False, server_default="0"),
    Column("last_access_at", UTCDateTime),
    CheckConstraint("kind IN ('file', 'dir')", name="ck_shares_links_kind"),
)
snapshots = Table(
    "shares_snapshot",
    metadata,
    Column(
        "share_id",
        Text,
        ForeignKey("shares_links.share_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("file_id", Text, primary_key=True),
)
access = Table(
    "shares_access",
    metadata,
    Column("access_id", Integer, primary_key=True, autoincrement=True),
    Column(
        "share_id", Text, ForeignKey("shares_links.share_id", ondelete="CASCADE"), nullable=False
    ),
    Column("at", UTCDateTime, nullable=False),
    Column("ip", Text, nullable=False),
    Column("user_agent", Text, nullable=False),
    Column("kind", Text, nullable=False),
)

AccessKind = Literal["view", "download", "zip", "password_failed"]
EndReason = Literal["revoked", "expired", "undone"]
Status = Literal["active", "revoked", "expired", "undone"]
EXPIRE_ACTOR = "shares.expire"


@dataclass(frozen=True, slots=True)
class Share:
    share_id: str
    token: str
    path: str
    kind: Literal["file", "dir"]
    password_hash: str | None
    expires_at: datetime | None
    snapshot: bool
    created_at: datetime
    created_by: str
    updated_at: datetime
    ended_at: datetime | None
    end_reason: EndReason | None
    ended_by: str | None
    access_count: int
    last_access_at: datetime | None

    @property
    def has_password(self) -> bool:
        return self.password_hash is not None

    @property
    def no_expiry(self) -> bool:
        return self.expires_at is None

    def status(self, now: datetime) -> Status:
        if self.end_reason is not None:
            return self.end_reason
        if self.expires_at is not None and self.expires_at <= now:
            return "expired"
        return "active"

    def active(self, now: datetime) -> bool:
        return self.status(now) == "active"

    def ended(self, now: datetime) -> datetime | None:
        """When it stopped working (an expiry the job hasn't recorded yet counts)."""
        if self.ended_at is not None:
            return self.ended_at
        if self.expires_at is not None and self.expires_at <= now:
            return self.expires_at
        return None


@dataclass(frozen=True, slots=True)
class Access:
    at: datetime
    ip: str
    user_agent: str
    kind: AccessKind


def _share(row: Any) -> Share:
    return Share(
        share_id=row.share_id,
        token=row.token,
        path=row.path,
        kind=row.kind,
        password_hash=row.password_hash,
        expires_at=row.expires_at,
        snapshot=bool(row.snapshot),
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        ended_at=row.ended_at,
        end_reason=row.end_reason,
        ended_by=row.ended_by,
        access_count=row.access_count,
        last_access_at=row.last_access_at,
    )


def get(conn: Connection, share_id: str) -> Share | None:
    row = conn.execute(select(links).where(links.c.share_id == share_id)).one_or_none()
    return _share(row) if row is not None else None


def by_token(state: StateAccess, token: str) -> Share | None:
    with state.read() as conn:
        row = conn.execute(select(links).where(links.c.token == token)).one_or_none()
    return _share(row) if row is not None else None


def _active(now: datetime) -> Any:
    return (links.c.ended_at.is_(None)) & (
        links.c.expires_at.is_(None) | (links.c.expires_at > now)
    )


def listing(
    conn: Connection,
    *,
    now: datetime,
    past: bool,
    text: str = "",
    since: datetime | None = None,
    limit: int = 500,
) -> list[Share]:
    """Active shares (newest first) or past ones (most recently ended first)."""
    query = select(links)
    if past:
        ended = func.coalesce(links.c.ended_at, links.c.expires_at)
        query = query.where(~_active(now)).order_by(ended.desc(), links.c.created_at.desc())
    else:
        query = query.where(_active(now)).order_by(links.c.created_at.desc())
    if text:
        # SQLite's lower() folds ASCII only; good enough for a filter box.
        query = query.where(func.instr(func.lower(links.c.path), text.lower()) > 0)
    if since is not None:
        query = query.where(links.c.created_at >= since)
    return [_share(r) for r in conn.execute(query.limit(limit))]


def count_no_expiry(state: StateAccess) -> int:
    with state.read() as conn:
        return int(
            conn.execute(
                select(func.count())
                .select_from(links)
                .where(links.c.ended_at.is_(None), links.c.expires_at.is_(None))
            ).scalar_one()
        )


def snapshot_ids(state: StateAccess, share_id: str) -> list[str]:
    with state.read() as conn:
        return [
            str(r.file_id)
            for r in conn.execute(
                select(snapshots.c.file_id).where(snapshots.c.share_id == share_id)
            )
        ]


def accesses(conn: Connection, share_id: str, *, limit: int = 200) -> list[Access]:
    rows = conn.execute(
        select(access)
        .where(access.c.share_id == share_id)
        .order_by(access.c.access_id.desc())
        .limit(limit)
    )
    return [Access(r.at, r.ip, r.user_agent, r.kind) for r in rows]


def record(
    state: StateAccess, share_id: str, kind: AccessKind, *, ip: str, user_agent: str, now: datetime
) -> None:
    with state.write() as conn:
        conn.execute(
            insert(access).values(
                share_id=share_id, at=now, ip=ip, user_agent=user_agent[:300], kind=kind
            )
        )
        if kind != "password_failed":
            conn.execute(
                update(links)
                .where(links.c.share_id == share_id)
                .values(access_count=links.c.access_count + 1, last_access_at=now)
            )


def failures(state: StateAccess, share_id: str, *, since: datetime, ip: str | None) -> int:
    query = (
        select(func.count())
        .select_from(access)
        .where(
            access.c.share_id == share_id,
            access.c.kind == "password_failed",
            access.c.at >= since,
        )
    )
    if ip is not None:
        query = query.where(access.c.ip == ip)
    with state.read() as conn:
        return int(conn.execute(query).scalar_one())


def insert_share(
    conn: Connection,
    *,
    share_id: str,
    token: str,
    path: str,
    kind: str,
    password_hash: str | None,
    expires_at: datetime | None,
    snapshot_files: Iterable[str] | None,
    actor: str,
    now: datetime,
) -> None:
    conn.execute(
        insert(links).values(
            share_id=share_id,
            token=token,
            path=path,
            kind=kind,
            password_hash=password_hash,
            expires_at=expires_at,
            snapshot=snapshot_files is not None,
            created_at=now,
            created_by=actor,
            updated_at=now,
        )
    )
    if snapshot_files is not None:
        ids = list(dict.fromkeys(snapshot_files))
        if ids:
            conn.execute(insert(snapshots), [{"share_id": share_id, "file_id": i} for i in ids])


def change(conn: Connection, share_id: str, *, now: datetime, **values: Any) -> bool:
    """Update a share that hasn't ended; False if it has (or doesn't exist)."""
    result = conn.execute(
        update(links)
        .where(links.c.share_id == share_id, _active(now))
        .values(updated_at=now, **values)
    )
    return result.rowcount == 1


def end(conn: Connection, share_id: str, *, reason: EndReason, actor: str, now: datetime) -> bool:
    result = conn.execute(
        update(links)
        .where(links.c.share_id == share_id, links.c.ended_at.is_(None))
        .values(ended_at=now, end_reason=reason, ended_by=actor, updated_at=now)
    )
    return result.rowcount == 1


def expire_due(state: StateAccess, *, now: datetime) -> int:
    """Record every share whose time ran out as ended (`expired`, at its expiry)."""
    with state.write() as conn:
        result = conn.execute(
            update(links)
            .where(
                links.c.ended_at.is_(None),
                links.c.expires_at.is_not(None),
                links.c.expires_at <= now,
            )
            .values(
                ended_at=links.c.expires_at,
                end_reason="expired",
                ended_by=EXPIRE_ACTOR,
                updated_at=now,
            )
        )
        return int(result.rowcount)


def export(conn: Connection) -> dict[str, list[dict[str, Any]]]:
    """Everything except the secrets (tokens and password hashes)."""
    columns = [c for c in links.c if c.name not in ("token", "password_hash")]
    has_password = links.c.password_hash.is_not(None).label("has_password")
    shares = [dict(r._mapping) for r in conn.execute(select(*columns, has_password))]
    return {
        "shares_links": shares,
        "shares_snapshot": [dict(r._mapping) for r in conn.execute(select(snapshots))],
        "shares_access": [dict(r._mapping) for r in conn.execute(select(access))],
    }


def since_days(now: datetime, days: int | None) -> datetime | None:
    return now - timedelta(days=days) if days else None
