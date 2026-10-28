# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Stage jobs and the jobs that plan them.

- `core.stage.<stage>` (on the stage's lane, payload `content_hash`): run one stage on
  one content, record the result in `stage_runs`, then plan what depends on it.
- `core.stages.plan` (`cpu`): plan the files named in new `file.*` events, read from
  the outbox with the planner's own cursor. The worker's housekeeping queues it when
  such events exist; folder and folder-type changes queue a full pass instead.
- `core.stages.full` (`cpu`): plan every present file. Runs daily, at worker start when
  the stage graph changed (new stage, version bump…), and after folder-type changes.
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Any

from sqlalchemy import Connection, func, select
from sqlalchemy.dialects.sqlite import insert

from steward.core import queue
from steward.core.config import Config
from steward.core.db import Databases, utcnow
from steward.core.db.schema import blobs, event_cursors, events, pipeline_meta, stage_runs
from steward.core.discovery.hashing import HASH_STAGE, HASH_STAGE_VERSION
from steward.core.discovery.watcher import SCAN_JOB
from steward.core.folders import load_policy
from steward.core.privacy import PrivacyRules
from steward.core.stages.context import Context
from steward.core.stages.graph import StageGraph
from steward.core.stages.planner import (
    Eligibility,
    Planner,
    RunRecord,
    blob_info,
    run_status,
    stage_job_type,
)
from steward.sdk import (
    Daily,
    FileChanged,
    JobContext,
    JobHandler,
    PermanentJobError,
    Registrations,
    Registry,
    Stage,
    StageContext,
    StageResult,
)

log = logging.getLogger(__name__)

PLAN_JOB = "core.stages.plan"
FULL_JOB = "core.stages.full"
FULL_DEDUPE = f"schedule:{FULL_JOB}"
"""Same key as the daily schedule's, so the triggers never queue two full passes."""
PLANNER_CURSOR = "core.stages.planner"
SIGNATURE_KEY = "stage_graph_signature"
EVENT_BATCH = 1000
ERROR_MAX = 500

FILE_EVENTS = ("file.discovered", "file.changed", "file.moved", "file.reappeared")
FULL_PASS_EVENTS = (
    "folder.type_changed",
    "folder.moved",
    "folder_type.created",
    "folder_type.updated",
    "folder_type.renamed",
    "folder_type.deleted",
)
RELEVANT_EVENTS = FILE_EVENTS + FULL_PASS_EVENTS


def register_hash_stage(r: Registry) -> None:
    """Declare discovery's hashing as the graph's root stage. Discovery computes it
    itself (it decides file identity), so its job is never queued."""

    def computed_by_discovery(ctx: StageContext) -> StageResult | None:
        raise PermanentJobError(f"{HASH_STAGE} is computed by discovery, not as a job")

    r.stage(
        HASH_STAGE,
        computed_by_discovery,
        version=HASH_STAGE_VERSION,
        produces=("hash", "mime"),
        lane="io",
        index="metadata",
    )


def register_stage_jobs(
    registrations: Registrations, dbs: Databases, graph: StageGraph, config: Config
) -> Planner:
    """Called after modules are loaded: one job type per stage, plus the planning jobs."""
    max_attempts = config.pipeline.jobs.max_attempts
    planner = Planner(
        dbs, graph, max_attempts=max_attempts, privacy=PrivacyRules.from_config(config.privacy)
    )
    r = Registry("core", None, registrations)
    for name, stage in graph.stages.items():
        if name != graph.root:
            r.job(stage_job_type(name), _stage_handler(stage, dbs, planner), lane=stage.lane)

    def plan(ctx: JobContext) -> None:
        planned = 0
        while True:
            with dbs.state.read() as conn:
                cursor = _cursor(conn) or 0
                rows = conn.execute(
                    select(events.c.event_id, events.c.type, events.c.payload)
                    .where(events.c.event_id > cursor)
                    .order_by(events.c.event_id)
                    .limit(EVENT_BATCH)
                ).all()
            if not rows:
                break
            file_ids = {
                str(r.payload["file_id"])
                for r in rows
                if r.type in FILE_EVENTS and "file_id" in r.payload
            }
            if any(r.type in FULL_PASS_EVENTS for r in rows):
                with dbs.state.write() as conn:
                    enqueue_full_pass(conn, max_attempts=max_attempts)
            planned += planner.plan_files(file_ids)
            with dbs.state.write() as conn:
                _set_cursor(conn, rows[-1].event_id, utcnow())
            ctx.renew_lease()
        if planned:
            log.info("stages planned", extra={"fields": {"jobs": planned}})

    def full(ctx: JobContext) -> None:
        planned = planner.plan_all(renew=ctx.renew_lease)
        with dbs.index.write() as conn:
            _set_meta(conn, SIGNATURE_KEY, graph.signature)
        log.info("full stage planning pass finished", extra={"fields": {"jobs": planned}})

    r.job(PLAN_JOB, plan, lane="cpu")
    r.job(FULL_JOB, full, lane="cpu")
    r.schedule(FULL_JOB, Daily(time(4, 20)), job=FULL_JOB)
    return planner


# --- triggers ---------------------------------------------------------------------


def tick(conn: Connection, *, max_attempts: int = 5, now: datetime | None = None) -> bool:
    """Housekeeping: queue `core.stages.plan` if relevant events arrived; otherwise move
    the planner's cursor past irrelevant ones. Call inside `state.write()`. The cursor
    starts at 0, so a first start plans every retained file event once."""
    now = now or utcnow()
    cursor = _cursor(conn) or 0
    relevant = conn.execute(
        select(events.c.event_id)
        .where(events.c.event_id > cursor, events.c.type.in_(RELEVANT_EVENTS))
        .order_by(events.c.event_id)
        .limit(1)
    ).scalar()
    if relevant is not None:
        queue.enqueue(
            conn, PLAN_JOB, lane="cpu", dedupe_key=PLAN_JOB, max_attempts=max_attempts, now=now
        )
        return True
    latest = conn.execute(select(func.max(events.c.event_id))).scalar()
    if latest is not None and latest > cursor:
        _set_cursor(conn, latest, now)
    return False


def enqueue_full_pass(conn: Connection, *, max_attempts: int = 5) -> int:
    return queue.enqueue(
        conn, FULL_JOB, lane="cpu", dedupe_key=FULL_DEDUPE, max_attempts=max_attempts
    )


def full_pass_needed(dbs: Databases, graph: StageGraph) -> bool:
    """Whether the stage graph changed since the last full pass (or there never was one)."""
    with dbs.index.read() as conn:
        stored = conn.execute(
            select(pipeline_meta.c.value).where(pipeline_meta.c.key == SIGNATURE_KEY)
        ).scalar()
    return stored != graph.signature


# --- running one stage ------------------------------------------------------------


def _stage_handler(stage: Stage, dbs: Databases, planner: Planner) -> JobHandler:
    graph = planner.graph

    def run(ctx: JobContext) -> None:
        content_hash = str(ctx.payload["content_hash"])
        fields: dict[str, Any] = {"stage": stage.name, "content_hash": content_hash}
        with dbs.index.read() as conn:
            row = (
                conn.execute(select(blobs).where(blobs.c.content_hash == content_hash))
                .mappings()
                .one_or_none()
            )
            runs = {
                r.stage: RunRecord(r.stage_version, r.status, r.updated_at)
                for r in conn.execute(
                    select(stage_runs).where(stage_runs.c.content_hash == content_hash)
                )
            }
        if row is None:
            return  # content no longer indexed
        blob = blob_info(row)
        current, usable = run_status(graph, runs)
        if stage.name in current and runs[stage.name].status != "failed":
            planner.plan_hashes([content_hash])  # e.g. a crash after recording the result
            return
        if not all(up in usable for up in graph.upstream(stage.name)):
            return  # inputs changed meanwhile; the planner queues it again when ready
        with dbs.state.read() as conn:
            policy = load_policy(conn)
            rows = planner.present_files(conn, [content_hash])[content_hash]
        candidates = Eligibility(graph, policy, planner.privacy).candidates(stage, blob, rows)
        if not candidates:
            return  # no longer wanted (file gone, folder type changed…)
        chosen = candidates[0]
        sctx = Context(
            stage=stage.name,
            blob=blob,
            file=chosen.file,
            folder=chosen.folder,
            attempt=ctx.attempt,
            root=chosen.root,
            dbs=dbs,
            renew=ctx.renew_lease,
        )
        try:
            result = stage.run(sctx) or StageResult()
            if not sctx.unchanged():
                raise FileChanged("file changed while the stage ran")
        except FileChanged:
            log.info("file changed under a stage; rescanning", extra={"fields": fields})
            with dbs.state.write() as conn:
                queue.enqueue(
                    conn,
                    SCAN_JOB,
                    lane="io",
                    payload={
                        "library_id": chosen.file.library_id,
                        "paths": [chosen.file.relpath],
                    },
                    max_attempts=planner.max_attempts,
                )
            return
        except Exception as exc:
            _record(
                dbs,
                content_hash,
                stage,
                status="failed",
                error=f"{type(exc).__name__}: {exc}"[:ERROR_MAX],
            )
            raise
        _record(
            dbs,
            content_hash,
            stage,
            status=result.status,
            error=result.reason[:ERROR_MAX] if result.reason else None,
            model=result.model,
            prompt_version=result.prompt_version,
        )
        log.debug("stage run", extra={"fields": {**fields, "status": result.status}})
        if graph.downstream[stage.name]:
            planner.plan_hashes([content_hash])

    return run


def _record(
    dbs: Databases,
    content_hash: str,
    stage: Stage,
    *,
    status: str,
    error: str | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
) -> None:
    values = {
        "stage_version": stage.version,
        "status": status,
        "error": error,
        "model": model,
        "prompt_version": prompt_version,
        "updated_at": utcnow(),
    }
    with dbs.index.write() as conn:
        conn.execute(
            insert(stage_runs)
            .values(content_hash=content_hash, stage=stage.name, **values)
            .on_conflict_do_update(
                index_elements=[stage_runs.c.content_hash, stage_runs.c.stage], set_=values
            )
        )


# --- small tables -----------------------------------------------------------------


def _cursor(conn: Connection) -> int | None:
    value = conn.execute(
        select(event_cursors.c.last_event_id).where(event_cursors.c.subscriber == PLANNER_CURSOR)
    ).scalar()
    return None if value is None else int(value)


def _set_cursor(conn: Connection, last_event_id: int, now: datetime) -> None:
    stmt = insert(event_cursors).values(
        subscriber=PLANNER_CURSOR, last_event_id=last_event_id, updated_at=now
    )
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[event_cursors.c.subscriber],
            set_={"last_event_id": last_event_id, "updated_at": now},
        )
    )


def _set_meta(conn: Connection, key: str, value: str) -> None:
    now = utcnow()
    conn.execute(
        insert(pipeline_meta)
        .values(key=key, value=value, updated_at=now)
        .on_conflict_do_update(
            index_elements=[pipeline_meta.c.key], set_={"value": value, "updated_at": now}
        )
    )
