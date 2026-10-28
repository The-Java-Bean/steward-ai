# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

import logging
from pathlib import Path

import pytest
import uvicorn
from support import make_runtime, use_fast_hashes

from steward.api import serve
from steward.core.db import migrate


def test_serve_migrates_prints_a_setup_token_and_stops_the_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    use_fast_hashes(monkeypatch)
    runtime = make_runtime(tmp_path, upgrade=False)
    ran: list[object] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self: ran.append(self.config.app))
    try:
        with caplog.at_level(logging.INFO):
            serve(runtime)
        assert ran, "uvicorn wasn't started"
        migrate.ensure_current(runtime.dbs.state, runtime.migration_owners("state"))
        assert any(r.getMessage().startswith("setup token: ") for r in caplog.records)

        # With an owner, no token is printed.
        runtime.auth.create_owner("owner", "correct horse battery")
        caplog.clear()
        with caplog.at_level(logging.INFO):
            serve(runtime)
        assert not any("setup token" in r.getMessage() for r in caplog.records)
    finally:
        runtime.close()
