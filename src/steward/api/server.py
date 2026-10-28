# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Running `steward api`: migrations, the setup token, the executor lane, Uvicorn."""

from __future__ import annotations

import logging
import threading

import uvicorn

from steward.api.app import create_app
from steward.core import backup
from steward.core.runtime import Runtime
from steward.core.setup import SetupToken
from steward.core.worker import Worker, new_worker_id

log = logging.getLogger(__name__)


def serve(runtime: Runtime) -> None:
    """Back up if a migration is pending (`backup.before_migrate`), upgrade the
    databases, then serve until SIGTERM/SIGINT. The executor lane runs in
    this process (it's the only writer); the other lanes belong to `steward worker`."""
    taken = backup.before_migrate(runtime)
    if taken is not None:
        log.info("backed up before migrating", extra={"fields": {"path": str(taken.path)}})
    changed = runtime.upgrade_databases()
    for database, owners in changed.items():
        if owners:
            log.info("migrated", extra={"fields": {"database": database, "owners": owners}})
    token = None
    if not runtime.auth.has_owner():
        token = SetupToken()
        log.warning(
            f"setup token: {token.value}",
            extra={"fields": {"hint": "open /setup in a browser and paste this token"}},
        )
    app = create_app(runtime, setup_token=token)
    api = runtime.config.steward.api
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=api.host,
            port=api.port,
            log_config=None,
            access_log=False,
            proxy_headers=False,  # X-Forwarded-For is handled per `trusted_proxies`
            server_header=False,
            date_header=False,
        )
    )
    stop = threading.Event()
    executor = Worker(runtime, lanes=("executor",), worker_id=new_worker_id("api"))
    executor_thread = threading.Thread(
        target=executor.run, args=(stop,), kwargs={"housekeeping": False}, name="executor"
    )
    executor_thread.start()
    log.info("api starting", extra={"fields": {"host": api.host, "port": api.port}})
    try:
        server.run()
    finally:
        stop.set()
        executor_thread.join(timeout=30)
