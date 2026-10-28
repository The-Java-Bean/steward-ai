# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""What the core itself registers: maintenance jobs, their schedules, migrations, the
root `core.hash` stage, and one delivery job type per event subscriber."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import time, timedelta
from typing import TYPE_CHECKING

from steward.core import events, queue
from steward.core.auth import Auth
from steward.core.auth.jobs import register_auth
from steward.core.config import Config
from steward.core.db import Databases, utcnow
from steward.core.db.migrate import CORE_VERSIONS
from steward.core.discovery.jobs import register_discovery
from steward.core.executor import register_actions
from steward.core.stages import register_hash_stage
from steward.sdk import (
    Daily,
    EventHandler,
    HealthResult,
    JobContext,
    JobHandler,
    Registrations,
    Registry,
)

if TYPE_CHECKING:
    from steward.core.runtime import Runtime

log = logging.getLogger(__name__)


def register_core(r: Registry, config: Config, dbs: Databases) -> None:
    jobs_cfg = config.pipeline.jobs
    events_cfg = config.pipeline.events

    def prune_jobs(ctx: JobContext) -> None:
        with dbs.state.write() as conn:
            removed = queue.prune_done(
                conn, older_than=utcnow() - timedelta(days=jobs_cfg.keep_done_days)
            )
        log.info("pruned finished jobs", extra={"fields": {"removed": removed}})

    def prune_events(ctx: JobContext) -> None:
        with dbs.state.write() as conn:
            removed = events.prune(
                conn, older_than=utcnow() - timedelta(days=events_cfg.retention_days)
            )
        log.info("pruned old events", extra={"fields": {"removed": removed}})

    r.job("core.prune_jobs", prune_jobs, lane="io")
    r.job("core.prune_events", prune_events, lane="io")
    r.schedule("core.prune_jobs", Daily(time(4, 0)), job="core.prune_jobs")
    r.schedule("core.prune_events", Daily(time(4, 10)), job="core.prune_events")
    register_discovery(r, config, dbs)
    register_hash_stage(r)
    register_auth(r, Auth(dbs.state, config.steward.auth))
    register_actions(r)
    r.migrations(CORE_VERSIONS["state"], database="state")
    r.migrations(CORE_VERSIONS["index"], database="index")


def register_deliveries(registrations: Registrations, dbs: Databases) -> None:
    """Give every subscriber a delivery job type on the `events` lane."""
    r = Registry("core", None, registrations)
    for subscriber, sub in registrations.subscriptions().items():
        r.job(
            events.delivery_job_type(subscriber),
            _deliver(dbs, subscriber, sub.handler),
            lane="events",
        )


def _deliver(dbs: Databases, subscriber: str, handler: EventHandler) -> JobHandler:
    def run(ctx: JobContext) -> None:
        event_id = int(ctx.payload["event_id"])
        with dbs.state.read() as conn:
            event = events.load(conn, event_id)
        if event is None:
            log.warning(
                "event no longer exists; skipping delivery",
                extra={"fields": {"event_id": event_id, "subscriber": subscriber}},
            )
            return
        handler(event)

    return run


BACKUP_JOB = "core.backup"
BACKUP_WARN_AFTER = timedelta(days=2)


def register_backup(
    registrations: Registrations, config: Config, runtime: Callable[[], Runtime]
) -> None:
    """The nightly backup (turn it off in `pipeline.yaml` with `core.backup: off` when an
    external tool runs `steward backup` instead) and its health check. Registered after
    the modules, because backups include every module's export."""
    from steward.core import backup

    r = Registry("core", None, registrations)

    def run(ctx: JobContext) -> None:
        backup.backup(runtime(), reason="scheduled")

    async def check() -> HealthResult:
        latest = backup.latest_backup(config.steward.backup_dir)
        if latest is None:
            return HealthResult("warn", "no backup yet; run `steward backup`")
        detail = f"last backup {latest.created_at:%Y-%m-%d %H:%M} UTC ({latest.reason})"
        if latest.age > BACKUP_WARN_AFTER:
            return HealthResult("warn", f"{detail}, more than 2 days ago")
        return HealthResult("ok", detail)

    r.job(BACKUP_JOB, run, lane="io")
    r.schedule(BACKUP_JOB, Daily(time(3, 30)), job=BACKUP_JOB)
    r.health_check("core.backup", check)
