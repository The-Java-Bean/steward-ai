# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

from pathlib import Path

from support import write_config
from typer.testing import CliRunner, Result

from steward.cli import app


def run(config: Path, *args: str) -> Result:
    return CliRunner().invoke(app, ["--config-dir", str(config), *args])


def config_dir(tmp_path: Path, **files: dict[str, object]) -> Path:
    steward = {"data_dir": str(tmp_path / "data"), **files.pop("steward", {})}
    return write_config(tmp_path / "config", steward=steward, **files)


def test_config_check(tmp_path: Path) -> None:
    result = run(
        config_dir(tmp_path, pipeline={"schedules": {"core.prune_events": "every 6h"}}),
        "config",
        "check",
    )
    assert result.exit_code == 0, result.output
    assert "schedule:   core.prune_events: every 6h" in result.output
    assert "schedule:   core.prune_jobs: daily 04:00" in result.output


def test_config_errors_exit_nonzero(tmp_path: Path) -> None:
    result = run(
        config_dir(tmp_path, pipeline={"schedules": {"core.typo": "off"}}), "config", "check"
    )
    assert result.exit_code == 1
    assert "unknown schedule(s): core.typo" in result.output


def test_enabling_a_missing_module_fails(tmp_path: Path) -> None:
    config = config_dir(tmp_path, modules={"modules": {"nope": {"enabled": True}}})
    result = run(config, "modules")
    assert result.exit_code == 1
    assert "isn't installed" in result.output


def test_db_upgrade_status_and_worker_precheck(tmp_path: Path) -> None:
    config = config_dir(tmp_path)
    assert "no database yet" in run(config, "db", "status").output
    worker = run(config, "worker")
    assert worker.exit_code == 1
    assert "steward db upgrade" in worker.output

    first = run(config, "db", "upgrade")
    assert first.exit_code == 0, first.output
    assert "state: upgraded core" in first.output
    assert "state: up to date" in run(config, "db", "upgrade").output
    status = run(config, "db", "status").output
    assert "state: core at " in status
    assert "needs upgrade" not in status


def test_jobs_list_and_retry(tmp_path: Path) -> None:
    config = config_dir(tmp_path)
    run(config, "db", "upgrade")
    assert run(config, "jobs", "list").output == ""
    result = run(config, "jobs", "retry", "99")
    assert result.exit_code == 1
    assert "isn't dead" in result.output


def test_folders_import_show_and_lock(tmp_path: Path) -> None:
    library = tmp_path / "files"
    library.mkdir()
    config = config_dir(
        tmp_path,
        libraries={"libraries": {"files": {"path": str(library)}}},
        folders={
            "types": {"receipts": {"extends": "documents", "fields": ["vendor"]}},
            "mappings": {
                "files": {"Receipts": "receipts", "Private": {"type": "private", "permanent": True}}
            },
        },
    )
    assert "steward db upgrade" in run(config, "folders", "list").output
    run(config, "db", "upgrade")
    imported = run(config, "folders", "import")
    assert imported.exit_code == 0, imported.output
    assert "libraries added: files" in imported.output
    assert "default folders assigned for: trash, edit_backups" in imported.output

    listing = run(config, "folders", "list").output
    assert "Private: private [locked, permanent]" in listing
    assert "role trash: files:.steward/trash" in listing

    shown = run(config, "folders", "show", "files:Receipts/2026").output
    assert "type:          receipts (mapped at files:Receipts" in shown
    assert "fields:        vendor" in shown

    refused = run(config, "folders", "unlock", "files:Private")
    assert refused.exit_code == 1
    assert "permanently locked" in refused.output
    assert run(config, "folders", "unlock", "--permanent", "files:Private").exit_code == 0

    assert run(config, "folders", "map", "files:Notes", "notes").exit_code == 0
    assert run(config, "folders", "move", "files:Notes", "Journal").exit_code == 0
    assert (
        "notes (mapped at files:Journal" in run(config, "folders", "show", "files:Journal").output
    )
    assert run(config, "folders", "show", "nope").exit_code == 1

    assert "receipts" in run(config, "types", "list").output
    assert "chain receipts > documents > general" in run(config, "types", "show", "receipts").output
    assert run(config, "types", "delete", "receipts").exit_code == 1  # in use


def test_scan_and_files_list(tmp_path: Path) -> None:
    library = tmp_path / "files"
    (library / "Docs").mkdir(parents=True)
    (library / "Docs" / "a.txt").write_text("hello")
    config = config_dir(
        tmp_path,
        libraries={"libraries": {"files": {"path": str(library)}}},
        folders={"mappings": {"files": {"Docs": "documents"}}},
    )
    run(config, "db", "upgrade")
    run(config, "folders", "import")
    scanned = run(config, "scan")
    assert scanned.exit_code == 0, scanned.output
    assert "files: files new 1" in scanned.output
    assert "files unchanged 1" in run(config, "scan", "files:Docs").output
    listing = run(config, "files", "list", "files:Docs").output
    assert "files:Docs/a.txt  5 B  b3:" in listing
    (library / "Docs" / "a.txt").unlink()
    (library / "Docs").rmdir()
    refused = run(config, "scan")
    assert refused.exit_code == 1
    assert "allow-empty" in refused.output
    assert run(config, "scan", "--allow-empty").exit_code == 0
    assert "[missing]" in run(config, "files", "list", "--missing").output
    assert run(config, "scan", "nope").exit_code == 1
