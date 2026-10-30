# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The worker process: lane threads that run jobs, plus a housekeeping thread that
dispatches events and fires schedules.

Each lane gets as many threads as its configured concurrency. Handlers run in those
threads. On stop, threads finish their current job and exit; a job cut off by a hard
kill is taken over by another worker when its lease expires.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta

from steward.core import events, queue, reindex, scheduler, stages
from steward.core.db import utcnow
from steward.core.discovery.ioprio import set_idle_io_priority
from steward.core.library import Library, ModuleActions
from steward.core.runtime import Runtime
from steward.sdk import WORKER_LANES, JobContext, Lane, PermanentJobError, RetryLater

log = logging.getLogger(__name__)


def new_worker_id(role: str = "worker") -> str:
    return f"{role}-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


class Worker:
    def __init__(
        self,
        runtime: Runtime,
        *,
        lanes: Iterable[Lane] = WORKER_LANES,
        worker_id: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.lanes = tuple(lanes)
        self.worker_id = worker_id or new_worker_id()
        jobs_cfg = runtime.config.pipeline.jobs
        self.lease = timedelta(seconds=jobs_cfg.lease_seconds)
        self.backoff = queue.Backoff(jobs_cfg.backoff_base_seconds, jobs_cfg.backoff_max_seconds)
        self.max_attempts = jobs_cfg.max_attempts
        self.poll_seconds = jobs_cfg.poll_seconds
        self.job_types = runtime.registrations.job_types()
        self.types_by_lane: dict[Lane, list[str]] = defaultdict(list)
        for name, jt in self.job_types.items():
            self.types_by_lane[jt.lane].append(name)

    # --- one step at a time (used by the loops, and directly by tests) -------------

    def run_one(self, lane: Lane, *, now: datetime | None = None) -> bool:
        """Claim and run one job on `lane`. Returns False if nothing was runnable."""
        state = self.runtime.dbs.state
        with state.write() as conn:
            job = queue.claim(
                conn,
                lane=lane,
                types=reindex.claimable(conn, self.types_by_lane.get(lane, ())),
                worker_id=self.worker_id,
                lease=self.lease,
                now=now,
            )
        if job is None:
            return False

        fields = {"job_id": job.job_id, "job_type": job.type, "lane": lane, "attempt": job.attempts}

        def renew_lease() -> None:
            with state.write() as conn:
                if not queue.renew(conn, job.job_id, worker_id=self.worker_id, lease=self.lease):
                    log.warning("lost the lease on a running job", extra={"fields": fields})

        ctx = JobContext(
            job.job_id,
            job.type,
            job.payload,
            job.attempts,
            renew_lease,
            state,
            library=Library(self.runtime.dbs, self.runtime.privacy),
            actions=(
                ModuleActions(self.runtime.executor, job.type.partition(".")[0])
                if lane == "executor"
                else None
            ),
        )
        try:
            self.job_types[job.type].handler(ctx)
        except RetryLater as exc:
            with state.write() as conn:
                queue.postpone(
                    conn,
                    job.job_id,
                    worker_id=self.worker_id,
                    delay=timedelta(seconds=exc.delay_s),
                    reason=exc.reason,
                )
            log.info(
                "job postponed",
                extra={"fields": {**fields, "reason": exc.reason, "delay_s": exc.delay_s}},
            )
            return True
        except Exception as exc:
            permanent = isinstance(exc, PermanentJobError)
            with state.write() as conn:
                status = queue.fail(
                    conn,
                    job.job_id,
                    worker_id=self.worker_id,
                    error=f"{type(exc).__name__}: {exc}",
                    backoff=self.backoff,
                    permanent=permanent,
                )
            log.warning(
                "job failed",
                exc_info=True,
                extra={"fields": {**fields, "status": status or "lost lease"}},
            )
            return True
        with state.write() as conn:
            done = queue.complete(conn, job.job_id, worker_id=self.worker_id)
        if done:
            log.debug("job done", extra={"fields": fields})
        else:
            log.warning("job finished after its lease was taken over", extra={"fields": fields})
        return True

    def housekeeping(self, *, now: datetime | None = None) -> None:
        """Dispatch new events, queue stage planning, and fire due schedules."""
        now = now or utcnow()
        with self.runtime.dbs.state.write() as conn:
            events.dispatch(
                conn,
                self.runtime.registrations.subscriptions(),
                max_attempts=self.max_attempts,
                now=now,
            )
            stages.tick(conn, max_attempts=self.max_attempts, now=now)
            fired = scheduler.tick(
                conn,
                self.runtime.schedules,
                self.job_types,
                tz=self.runtime.config.steward.tz,
                max_attempts=self.max_attempts,
                now=now,
            )
        for name in fired:
            log.info("schedule fired", extra={"fields": {"schedule": name}})
        stages.complete_spaces(self.runtime.dbs, self.runtime.stages, now=now)
        reindex.settle(self.runtime.dbs)

    def drain(self, *, max_jobs: int = 10_000) -> int:
        """Housekeeping, then run jobs on every lane until none are runnable. Synchronous;
        meant for tests and one-off maintenance. Returns the number of jobs run."""
        ran = 0
        while ran < max_jobs:
            self.housekeeping()
            progressed = False
            for lane in self.lanes:
                while ran < max_jobs and self.run_one(lane):
                    ran += 1
                    progressed = True
            if not progressed:
                return ran
        return ran

    # --- the long-running process ---------------------------------------------------

    def run(
        self, stop: threading.Event, *, grace_seconds: float = 30, housekeeping: bool = True
    ) -> None:
        """Run until `stop` is set. `housekeeping=False` runs only the lane threads (the
        API process runs the executor lane; dispatch and schedules stay in the worker)."""
        threads = []
        if housekeeping:
            threads.append(
                threading.Thread(target=self._housekeeping_loop, args=(stop,), name="housekeeping")
            )
        for lane in self.lanes:
            for i in range(self.runtime.config.pipeline.concurrency(lane)):
                threads.append(
                    threading.Thread(target=self._lane_loop, args=(lane, stop), name=f"{lane}-{i}")
                )
        log.info(
            "worker started",
            extra={"fields": {"worker_id": self.worker_id, "threads": len(threads)}},
        )
        for t in threads:
            t.start()
        stop.wait()
        log.info("worker stopping", extra={"fields": {"worker_id": self.worker_id}})
        for t in threads:
            t.join(timeout=grace_seconds)
        log.info("worker stopped", extra={"fields": {"worker_id": self.worker_id}})

    def _lane_loop(self, lane: Lane, stop: threading.Event) -> None:
        if lane == "io" and self.runtime.config.pipeline.discovery.idle_io_priority:
            set_idle_io_priority()
        while not stop.is_set():
            try:
                ran = self.run_one(lane)
            except Exception:
                log.exception("lane loop error", extra={"fields": {"lane": lane}})
                ran = False
            if not ran:
                stop.wait(self.poll_seconds)

    def _housekeeping_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.housekeeping()
            except Exception:
                log.exception("housekeeping error")
            stop.wait(self.poll_seconds)
