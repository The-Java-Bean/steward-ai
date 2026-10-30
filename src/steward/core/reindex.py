# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Rebuilding `index.db` from the libraries (docs/adr/0028).

`request` marks a rebuild in `state.db` and queues `core.index.rebuild`. The job empties
`index.db` in place, in one transaction, and scans every library; scans restore the
blobs of unchanged files (`Scanner._restore`) and the planner does the rest. Housekeeping
(`settle`) ends the rebuild when nothing is left to do.

`index.db` is emptied rather than deleted because the API and worker keep connections
to it: their writes would go to a deleted file, and when those connections close SQLite
removes `index.db-wal` and `-shm` by name, whichever database they belong to by then.

While `rebuilding` is true the index is incomplete on purpose. Nothing may act on what
it's missing: sidecars, for one, would lose the tags and descriptions not recomputed yet.

Emptying takes minutes on a large index on a disk (167 s for 500,000 chunks with vectors
on an HDD: SQLite has to visit every page), far longer than a writer waits for the lock.
So no other job is claimed meanwhile (`claimable`), and the job's lease is kept alive from
a thread. Readers aren't affected: until it commits, search sees the old index.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Connection, func, select, text
from sqlalchemy.exc import DatabaseError, DBAPIError, OperationalError

from steward.core import queue, settings, stages
from steward.core.config import Config
from steward.core.db import Database, Databases
from steward.core.db.migrate import MigrationError
from steward.core.db.schema import (
    ACTIVE_JOB_STATUSES,
    blobs,
    files,
    jobs,
    libraries,
    settings_kv,
    stage_runs,
)
from steward.core.discovery.jobs import RECONCILE_JOB
from steward.core.discovery.watcher import SCAN_JOB
from steward.core.stages import FULL_JOB, PLAN_JOB, STAGE_JOB_PREFIX, StageGraph
from steward.sdk import JobContext, PermanentJobError, Registrations, Registry

log = logging.getLogger(__name__)

REBUILD_JOB = "core.index.rebuild"
SETTING = "index.rebuild"
ACTOR = "system:reindex"
RECOVERY = (
    "stop the API and worker, delete index.db and its -wal and -shm files, run "
    "`steward reindex`, then start them again"
)
LEASE_RENEW_S = 60.0
_BUSY_TYPES = (REBUILD_JOB, SCAN_JOB, RECONCILE_JOB, PLAN_JOB, FULL_JOB)


def stale_journal(db: Database) -> list[str]:
    """`-wal`/`-shm` files of a database file that's gone: something still has it open,
    or a crash left them, and a new file must not pick them up."""
    if db.exists():
        return []
    return [
        p.name
        for p in (db.path.with_name(db.path.name + s) for s in ("-wal", "-shm"))
        if p.exists()
    ]


def rebuilding(conn: Connection) -> bool:
    """`conn` is on `state.db`."""
    return (
        conn.execute(select(settings_kv.c.key).where(settings_kv.c.key == SETTING)).first()
        is not None
    )


def lost(dbs: Databases) -> bool:
    """Files have hashes but `index.db` has no blobs at all: it was lost or replaced."""
    with dbs.state.read() as conn:
        hashed = conn.execute(
            select(files.c.file_id).where(files.c.content_hash.is_not(None)).limit(1)
        ).first()
    if hashed is None:
        return False
    with dbs.index.read() as conn:
        return conn.execute(select(blobs.c.content_hash).limit(1)).first() is None


@contextmanager
def naming_recovery(name: str) -> Iterator[None]:
    """A damaged `index.db` stops the API and worker before any job could say what to do.
    `OperationalError` isn't damage: it's "database is locked" and the like."""
    try:
        yield
    except DatabaseError as exc:
        if name != "index" or isinstance(exc, OperationalError):
            raise
        raise MigrationError(f"index.db is damaged: {RECOVERY}") from exc


def claimable(conn: Connection, types: Sequence[str]) -> Sequence[str]:
    """The job types a worker may start now: while `index.db` is being emptied, only the
    job doing that (again, if a hard kill interrupted it). `conn` is on `state.db`."""
    mark = conn.execute(select(settings_kv.c.value).where(settings_kv.c.key == SETTING)).scalar()
    if not mark or not mark.get("emptying"):
        return types
    alive = conn.execute(
        select(jobs.c.job_id).where(
            jobs.c.type == REBUILD_JOB, jobs.c.status.in_(ACTIVE_JOB_STATUSES)
        )
    ).first()
    return [t for t in types if t == REBUILD_JOB] if alive is not None else types


def _set_emptying(dbs: Databases, emptying: bool) -> None:
    with dbs.state.write() as conn:
        settings.set_value(conn, SETTING, {"emptying": emptying}, actor=ACTOR)


@contextmanager
def _keep_lease(ctx: JobContext) -> Iterator[None]:
    """Renew the job's lease while one long statement runs, or another worker would take
    the job over and start it again."""
    stop = threading.Event()

    def renew() -> None:
        while not stop.wait(LEASE_RENEW_S):
            ctx.renew_lease()

    thread = threading.Thread(target=renew, name="reindex-lease", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def request(conn: Connection, *, actor: str, max_attempts: int = 5) -> int:
    """Mark a rebuild and queue its job, atomically: housekeeping can't see the mark
    without the job. `conn` is a write transaction on `state.db`."""
    settings.set_value(conn, SETTING, {"emptying": False}, actor=actor)
    return queue.enqueue(
        conn, REBUILD_JOB, lane="cpu", dedupe_key=REBUILD_JOB, max_attempts=max_attempts
    )


def reset(db: Database) -> dict[str, int]:
    """Empty every table in `db`, keeping the schema. Returns rows removed per table.

    One transaction, so readers see the old index or an empty one, and writers wait.
    Triggers are dropped and recreated around it, and children emptied before parents,
    so SQLite truncates tables instead of visiting each row (an FTS index would otherwise
    be updated per row while everyone else waits for the lock).
    """
    removed: dict[str, int] = {}
    with db.write() as conn:
        tables = conn.exec_driver_sql("PRAGMA main.table_list").all()
        plain = [t.name for t in tables if t.type == "table" and _is_data(t.name)]
        virtual = [t.name for t in tables if t.type == "virtual"]
        triggers = conn.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
        ).all()
        for name, _ in triggers:
            conn.exec_driver_sql(f'DROP TRIGGER "{name}"')
        for name in virtual:
            _empty_virtual(conn, name)
        for name in _children_first(conn, plain):
            removed[name] = conn.exec_driver_sql(f'DELETE FROM "{name}"').rowcount  # noqa: S608
        for _, sql in triggers:
            conn.exec_driver_sql(sql)
    return removed


def _is_data(name: str) -> bool:
    return not name.startswith(("sqlite_", "alembic_version_"))


def _empty_virtual(conn: Connection, name: str) -> None:
    """Search indexes that keep their text elsewhere can't be emptied with DELETE."""
    sql = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE name = :name"), {"name": name}
    ).scalar()
    if "fts5" in (sql or "").lower() and "content=" in (sql or "").lower().replace(" ", ""):
        conn.exec_driver_sql(f"""INSERT INTO "{name}"("{name}") VALUES ('delete-all')""")  # noqa: S608
    else:
        conn.exec_driver_sql(f'DELETE FROM "{name}"')  # noqa: S608


def _children_first(conn: Connection, tables: Iterable[str]) -> list[str]:
    names = list(tables)
    parents = {
        name: {
            row.table
            for row in conn.exec_driver_sql(f"""PRAGMA foreign_key_list("{name}")""")
            if row.table != name
        }
        for name in names
    }
    ordered: list[str] = []
    while names:
        ready = [n for n in names if not any(n in parents[o] for o in names if o != n)]
        for name in ready or names:  # `or`: a cycle; the foreign keys sort that out
            ordered.append(name)
            names.remove(name)
    return ordered


def settle(dbs: Databases) -> bool:
    """Housekeeping: end the rebuild once no job can add to the index any more. Jobs
    waiting for a model count, so it lasts until they've run. Returns True if it ended."""
    with dbs.state.write() as conn:
        if not rebuilding(conn) or stages.plan_pending(conn) or _busy(conn) > 0:
            return False
        settings.clear(conn, SETTING, actor=ACTOR)
    log.info("index rebuild finished")
    return True


def _busy(conn: Connection) -> int:
    return int(
        conn.execute(
            select(func.count())
            .select_from(jobs)
            .where(
                jobs.c.status.in_(ACTIVE_JOB_STATUSES),
                jobs.c.type.in_(_BUSY_TYPES) | jobs.c.type.startswith(STAGE_JOB_PREFIX),
            )
        ).scalar_one()
    )


@dataclass(frozen=True, slots=True)
class Summary:
    """What's in the index now, and what a rebuild couldn't bring back yet."""

    contents: int
    """Contents the libraries hold, by their hash."""
    indexed: int
    """Of those, how many `index.db` knows."""
    results: dict[str, int] = field(default_factory=dict)
    """Stage -> finished results."""
    not_running: dict[str, str] = field(default_factory=dict)
    """Stage with results -> why it isn't running now, so it wouldn't get them back."""
    rebuilding: bool = False
    since: datetime | None = None
    jobs_left: int = 0
    failed: int = 0
    """Stage results that failed."""


def summary(dbs: Databases, graph: StageGraph) -> Summary:
    with dbs.state.read() as conn:
        hashes = set(
            conn.execute(
                select(files.c.content_hash)
                .where(files.c.present.is_(True), files.c.content_hash.is_not(None))
                .distinct()
            ).scalars()
        )
        since = conn.execute(
            select(settings_kv.c.updated_at).where(settings_kv.c.key == SETTING)
        ).scalar()
        busy = _busy(conn)
    with dbs.index.read() as conn:
        known = set(conn.execute(select(blobs.c.content_hash)).scalars())
        rows = conn.execute(
            select(stage_runs.c.stage, stage_runs.c.status, func.count())
            .group_by(stage_runs.c.stage, stage_runs.c.status)
            .order_by(stage_runs.c.stage)
        ).all()
    results = {stage: n for stage, status, n in rows if status == "done"}
    return Summary(
        contents=len(hashes),
        indexed=len(hashes & known),
        results=results,
        not_running={s: graph.inactive[s] for s in results if s in graph.inactive},
        rebuilding=since is not None,
        since=since,
        jobs_left=busy,
        failed=sum(n for _, status, n in rows if status == "failed"),
    )


def register_reindex(registrations: Registrations, dbs: Databases, config: Config) -> None:
    max_attempts = config.pipeline.jobs.max_attempts
    r = Registry("core", None, registrations)

    def rebuild(ctx: JobContext) -> None:
        _set_emptying(dbs, True)
        try:
            with _keep_lease(ctx):
                removed = reset(dbs.index)
        except (DBAPIError, sqlite3.DatabaseError) as exc:
            cause = exc.orig if isinstance(exc, DBAPIError) else exc
            if isinstance(cause, sqlite3.OperationalError):
                raise  # "database is locked" and the like: the job's retries deal with it
            raise PermanentJobError(f"index.db is damaged: {RECOVERY}") from exc
        finally:
            _set_emptying(dbs, False)
        log.info("index emptied", extra={"fields": {"rows": sum(removed.values())}})
        with dbs.state.write() as conn:
            for library_id in conn.execute(select(libraries.c.library_id)).scalars():
                _enqueue_scan(conn, library_id, max_attempts)
            stages.enqueue_full_pass(conn, max_attempts=max_attempts)

    r.job(REBUILD_JOB, rebuild, lane="cpu")


def _enqueue_scan(conn: Connection, library_id: str, max_attempts: int) -> None:
    """With its own key: a scan already running may have gone past files before the
    reset, so it can't stand in for this one."""
    queue.enqueue(
        conn,
        SCAN_JOB,
        lane="io",
        payload={"library_id": library_id, "paths": [""]},
        dedupe_key=f"reindex:{library_id}",
        max_attempts=max_attempts,
    )
