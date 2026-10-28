# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""A separately installed module is found through its entry point (Phase 0 acceptance)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from support import write_config
from typer.testing import CliRunner

from steward.cli import app
from steward.core.loader import discover, load_modules, resolve_enabled
from steward.sdk import HealthResult


def test_installed_example_module_is_discovered() -> None:
    assert "hello" in discover()


def test_installed_modules_are_off_unless_enabled() -> None:
    enabled, _ = resolve_enabled({})
    assert "hello" not in enabled


def test_example_module_loads_and_registers() -> None:
    loaded = load_modules({"hello": {"greeting": "hi there"}})

    assert loaded.names() == ["hello"]
    assert loaded.modules[0].provider == "steward-hello"
    check = loaded.registrations.health_checks()["hello"]
    assert asyncio.run(check()) == HealthResult("ok", "hi there")


def test_cli_lists_the_example_module(tmp_path: Path) -> None:
    config = write_config(
        tmp_path / "config",
        steward={"data_dir": str(tmp_path / "data")},
        modules={"modules": {"hello": {"enabled": True}}},
    )
    result = CliRunner().invoke(app, ["--config-dir", str(config), "modules"])
    assert result.exit_code == 0, result.output
    assert "hello 0.3.0 (enabled; feature; installed; requires: -)" in result.output
    assert "health_check: hello" in result.output
