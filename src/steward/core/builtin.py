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
from steward.core.tags.actions import register_tag_actions
from steward.sdk import (
    TAGS_ARTIFACT,
    Daily,
    Event,
    EventHandler,
    Every,
    HealthResult,
    JobContext,
    JobHandler,
    Registrations,
    Registry,
)

if TYPE_CHECKING:
    from steward.core.runtime import Runtime
    from steward.core.stages import StageGraph

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
    register_tag_actions(r)
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


UPLOADS_EXPIRE_JOB = "core.uploads.expire"


def register_uploads(registrations: Registrations, runtime: Callable[[], Runtime]) -> None:
    """Hourly removal of abandoned uploads. Its lane is `executor`, so it runs in
    `steward api` (the worker mounts libraries read-only)."""
    r = Registry("core", None, registrations)

    def run(ctx: JobContext) -> None:
        removed = runtime().uploads.expire()
        if removed:
            log.info("expired unfinished uploads", extra={"fields": {"removed": removed}})

    r.job(UPLOADS_EXPIRE_JOB, run, lane="executor")
    r.schedule(UPLOADS_EXPIRE_JOB, Every(timedelta(hours=1)), job=UPLOADS_EXPIRE_JOB)


def register_models(registrations: Registrations, runtime: Callable[[], Runtime]) -> None:
    """Health checks for model roles and embeddings (docs/adr/0023). Registered after
    the stage graph is built, because they report what it left out."""
    from steward.core import embeddings

    r = Registry("core", None, registrations)

    async def models() -> HealthResult:
        graph = runtime().stages
        if not graph.roles:
            return HealthResult("ok", "no enabled module uses a model")
        off = []
        for name, state in graph.roles.items():
            if state.usable:
                continue
            stages = sorted(s for s in graph.inactive if f"role {name!r}" in graph.inactive[s])
            if not stages and not state.endpoint:
                continue  # used on request only (`steward tags bootstrap`), not by stages
            what = f", so {', '.join(stages)} doesn't run" if stages else ""
            off.append(f"role {name!r} ({', '.join(state.modules)}) is {state.problem}{what}")
        if off:
            return HealthResult("warn", "; ".join(off))
        mapped = ", ".join(f"{n} → {s.model}" for n, s in sorted(graph.roles.items()))
        return HealthResult("ok", mapped)

    async def spaces() -> HealthResult:
        rt = runtime()
        roles = [s for s in rt.stages.roles.values() if s.kind == "embed" and s.usable]
        if not roles:
            return HealthResult("ok", "no embedding model is set up; search works by words")
        parts = []
        with rt.dbs.index.read() as conn:
            for state in roles:
                space = embeddings.find_space(conn, state.name, state.model_key or "")
                if space is None:
                    parts.append(f"{state.name}: nothing embedded yet")
                    continue
                n = embeddings.count(conn, space.space_id)
                status = "complete" if space.status == "complete" else "filling"
                parts.append(f"{state.name}: {n} passages embedded with {space.model} ({status})")
        return HealthResult("ok", "; ".join(parts))

    r.health_check("core.models", models)
    r.health_check("core.embeddings", spaces)


RETAG_SUBSCRIBER = "core.tags.retag_untagged"


def tag_stage(graph: StageGraph) -> str | None:
    """The active stage that chooses tags (produces `tags`), if any."""
    return graph.producers.get(TAGS_ARTIFACT)


def register_tags(registrations: Registrations, runtime: Callable[[], Runtime]) -> None:
    """When a tag becomes usable, run the tag stage again where it was skipped (for
    example "no tags yet") or chose no tag; tags it already chose stay (docs/adr/0025).
    Registered before the delivery jobs are made."""
    from sqlalchemy import select

    from steward.core.db.schema import stage_runs, tag_suggestions
    from steward.core.stages import rerun
    from steward.core.tags import VOCABULARY_CHANGED

    r = Registry("core", None, registrations)
    untagged = stage_runs.c.content_hash.not_in(select(tag_suggestions.c.content_hash))

    def retag(event: Event) -> None:
        rt = runtime()
        stage = tag_stage(rt.stages)
        if stage is not None:
            rerun(rt.dbs, stage, statuses=("skipped",), also=untagged)

    r.subscribe(VOCABULARY_CHANGED, retag, name=RETAG_SUBSCRIBER)
