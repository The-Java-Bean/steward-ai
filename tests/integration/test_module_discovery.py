# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Phase 0 acceptance: a separately installed module is found through its entry point."""

from __future__ import annotations

import asyncio

from typer.testing import CliRunner

from steward.cli import app
from steward.core.loader import discover, load_modules
from steward.sdk import HealthResult


def test_installed_example_module_is_discovered() -> None:
    assert "hello" in discover()


def test_example_module_loads_and_registers() -> None:
    loaded = load_modules({"hello": {"greeting": "hi there"}})

    assert loaded.names() == ["hello"]
    assert loaded.modules[0].provider == "steward-hello"
    check = loaded.registrations.health_checks()["hello"]
    assert asyncio.run(check()) == HealthResult("ok", "hi there")


def test_cli_lists_the_example_module() -> None:
    result = CliRunner().invoke(app, ["modules"])
    assert result.exit_code == 0, result.output
    assert "hello 0.1.0 (feature; requires: -)" in result.output
    assert "health_check: hello" in result.output
