# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Auth housekeeping: the daily prune job and the `core.auth` health check."""

from __future__ import annotations

import logging
from datetime import time, timedelta

from steward.core.auth.service import Auth
from steward.core.db import utcnow
from steward.sdk import Daily, HealthResult, JobContext, Registry

log = logging.getLogger(__name__)

PRUNE_JOB = "core.auth.prune"


def register_auth(r: Registry, auth: Auth) -> None:
    def prune(ctx: JobContext) -> None:
        attempts, ended = auth.prune()
        log.info(
            "pruned login attempts and ended sessions",
            extra={"fields": {"attempts": attempts, "sessions": ended}},
        )

    async def health() -> HealthResult:
        try:
            return check_health(auth)
        except Exception as exc:  # a health check must not raise
            return HealthResult("fail", f"auth check failed: {type(exc).__name__}")

    r.job(PRUNE_JOB, prune, lane="io")
    r.schedule(PRUNE_JOB, Daily(time(4, 30)), job=PRUNE_JOB)
    r.health_check("core.auth", health)


def check_health(auth: Auth) -> HealthResult:
    """Warn when setup isn't done, re-auth is off, or password guessing looks likely."""
    if not auth.has_owner():
        return HealthResult("warn", "no owner account yet; finish setup")
    failures = auth.failed_attempts(utcnow() - timedelta(hours=24))
    notes = [f"{len(failures)} failed password check(s) in the last 24 hours"]
    warn = len(failures) >= auth.config.login.max_failures_per_ip
    if not auth.settings().reauth_enabled:
        warn = True
        notes.insert(0, "re-auth is turned off: locked folders need only a normal login")
    return HealthResult("warn" if warn else "ok", "; ".join(notes))
