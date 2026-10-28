# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from support import make_runtime, use_fast_hashes, write_config
from typer.testing import CliRunner

from steward.cli import app
from steward.core import backup
from steward.core.config import load_config
from steward.core.db import migrate
from steward.core.folders import FolderRef, store
from steward.core.runtime import build_runtime

ACTOR = "user:1"


@pytest.fixture(autouse=True)
def fast_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    use_fast_hashes(monkeypatch)


def test_backup_copies_databases_exports_state_and_prunes(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, steward={"backup": {"keep": 2}})
    try:
        runtime.auth.create_owner("owner", "correct-horse-battery", actor=ACTOR)
        with runtime.dbs.state.write() as conn:
            store.upsert_library(conn, "lib", root=str(tmp_path), actor=ACTOR)
            store.set_type(conn, FolderRef("lib", "x"), "private", actor=ACTOR)
        infos = [backup.backup(runtime) for _ in range(3)]
        kept = backup.list_backups(runtime.config.steward.backup_dir)
        assert [b.path for b in kept] == [infos[2].path, infos[1].path]
        assert not infos[0].path.exists()
        latest = infos[2].path
        assert sorted(p.name for p in latest.iterdir()) == [
            "index.db",
            "manifest.json",
            "state.db",
            "state.json",
        ]
        assert oct(latest.joinpath("state.db").stat().st_mode & 0o777) == "0o600"
        with sqlite3.connect(latest / "state.db") as conn:
            assert conn.execute("SELECT count(*) FROM folder_mappings").fetchone()[0] == 1
        exported = json.loads((latest / "state.json").read_text())
        assert exported["format"] == "steward-export"
        assert exported["core"]["users"][0]["username"] == "owner"
        assert "password_hash" not in exported["core"]["users"][0]
        assert "sessions" not in exported["core"]
        assert "jobs" not in exported["core"]
        manifest = json.loads((latest / "manifest.json").read_text())
        assert manifest["migrations"]["state"]["core"] == "0005"
        assert set(manifest["files"]) == {"index.db", "state.db", "state.json"}
    finally:
        runtime.close()


def test_module_exporters_are_included(tmp_path: Path) -> None:
    from support import make_module

    from steward.sdk import Registry

    def register(r: Registry) -> None:
        r.exporter(lambda conn: {"extra_things": [{"id": 1, "name": "kept"}]})

    runtime = make_runtime(tmp_path, [make_module("extra", register=register)])
    try:
        data = backup.export_state(runtime)
        assert data["modules"] == {"extra": {"extra_things": [{"id": 1, "name": "kept"}]}}
    finally:
        runtime.close()


def test_a_backup_is_taken_before_migrating_existing_databases(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    try:
        assert backup.before_migrate(runtime) is None  # up to date
        owners = runtime.migration_owners("state")
        migrate.downgrade(runtime.dbs.state, "core", owners["core"], "0004")
        taken = backup.before_migrate(runtime)
        assert taken is not None
        assert taken.reason == "pre-migrate"
        exported = json.loads((taken.path / "state.json").read_text())
        assert "core.journal: missing" in exported["errors"]
        assert backup.before_migrate(runtime) is None  # a recent backup exists
        runtime.upgrade_databases()
    finally:
        runtime.close()
    fresh = build_runtime(
        load_config(write_config(tmp_path / "fresh", steward={"data_dir": str(tmp_path / "d2")}))
    )
    try:
        assert backup.before_migrate(fresh) is None  # nothing to lose yet
    finally:
        fresh.close()


def test_cli_backup_and_export(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    runtime.close()
    runner = CliRunner()
    config = str(tmp_path / "config")
    result = runner.invoke(app, ["--config-dir", config, "backup"])
    assert result.exit_code == 0, result.output
    assert "/backups/" in result.output
    out = tmp_path / "export.json"
    result = runner.invoke(app, ["--config-dir", config, "export", "--output", str(out)])
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["format"] == "steward-export"
