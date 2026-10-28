# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Discovery's job types and schedule: `core.reconcile` (every 6h by default, and when
the worker starts) queues a full `core.scan` of each library; the watcher queues
`core.scan` jobs for the paths that changed; a library that is added (or gets a new
root) is scanned right away."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import Connection, select

from steward.core import queue
from steward.core.config import Config
from steward.core.db import Databases
from steward.core.db.schema import libraries
from steward.core.discovery.scanner import Scanner
from steward.core.discovery.watcher import SCAN_JOB
from steward.core.folders import PathError
from steward.core.privacy import PrivacyRules
from steward.sdk import Event, Every, JobContext, PermanentJobError, Registry

log = logging.getLogger(__name__)

RECONCILE_JOB = "core.reconcile"
RECONCILE_EVERY = timedelta(hours=6)


def register_discovery(r: Registry, config: Config, dbs: Databases) -> None:
    settings = config.pipeline.discovery
    privacy = PrivacyRules.from_config(config.privacy)
    max_attempts = config.pipeline.jobs.max_attempts

    def reconcile(ctx: JobContext) -> None:
        with dbs.state.write() as conn:
            for library_id in conn.execute(select(libraries.c.library_id)).scalars():
                enqueue_full_scan(conn, library_id, max_attempts=max_attempts)

    def scan(ctx: JobContext) -> None:
        library_id = str(ctx.payload["library_id"])
        paths = [str(p) for p in ctx.payload.get("paths", [""])]
        scanner = Scanner(dbs, settings, renew=ctx.renew_lease, privacy=privacy)
        try:
            report = scanner.scan(library_id, paths)
        except PathError as exc:
            raise PermanentJobError(str(exc)) from exc
        log.info(
            "scan finished",
            extra={"fields": {"library_id": library_id, "paths": len(paths), **report.as_dict()}},
        )

    def library_changed(event: Event) -> None:
        library_id = str(event.payload["library_id"])
        with dbs.state.write() as conn:
            enqueue_full_scan(conn, library_id, max_attempts=max_attempts)

    r.job(RECONCILE_JOB, reconcile, lane="io")
    r.subscribe("library.added", library_changed, name="core.scan_added_library")
    r.subscribe("library.updated", library_changed, name="core.scan_moved_library")
    r.job(SCAN_JOB, scan, lane="io")
    r.schedule(RECONCILE_JOB, Every(RECONCILE_EVERY), job=RECONCILE_JOB)


def enqueue_full_scan(conn: Connection, library_id: str, *, max_attempts: int = 5) -> int:
    return queue.enqueue(
        conn,
        SCAN_JOB,
        lane="io",
        payload={"library_id": library_id, "paths": [""]},
        dedupe_key=f"scan:{library_id}",
        max_attempts=max_attempts,
    )


def enqueue_scan(
    conn: Connection,
    library_id: str,
    paths: list[str],
    *,
    priority: int = 10,
    max_attempts: int = 5,
) -> int:
    """Scan some paths of a library soon (after a change steward-ai made or was told of)."""
    return queue.enqueue(
        conn,
        SCAN_JOB,
        lane="io",
        payload={"library_id": library_id, "paths": paths},
        priority=priority,
        max_attempts=max_attempts,
    )
