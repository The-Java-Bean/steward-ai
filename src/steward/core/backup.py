# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`steward backup` and `steward export` (plan §16.4).

A backup is a folder `<backup_dir>/<UTC timestamp>/` with consistent copies of
`state.db` and `index.db` (SQLite's online backup API, safe while steward-ai runs),
`state.json` (the export), and `manifest.json` (versions, migration heads, file sizes
and SHA-256). It's written under a temporary name and renamed when complete, so a
folder with a manifest is always a whole backup. The newest `backup.keep` are kept.

The export holds what can't be rebuilt, as plain JSON: libraries, folder types,
folders and mappings, roles, files and their path history, per-file privacy overrides,
the journal, runtime settings, accounts (without password hashes) and API tokens
(without their secrets), plus every module's exporter. It leaves out what's temporary
or secret: jobs, events, sessions, login attempts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, inspect, select

import steward
from steward.core.db import Database, utcnow
from steward.core.db.schema import state_metadata

if TYPE_CHECKING:
    from steward.core.runtime import Runtime

log = logging.getLogger(__name__)

EXPORT_FORMAT = "steward-export"
EXPORT_VERSION = 1
CORE_EXPORT_TABLES: dict[str, tuple[str, ...]] = {
    # table: columns left out
    "libraries": (),
    "folder_types": (),
    "folders": (),
    "folder_mappings": (),
    "folder_roles": (),
    "files": (),
    "path_history": (),
    "file_sensitivity": (),
    "tags": (),
    "file_tags": (),
    "journal": (),
    "eval_runs": (),
    "settings_kv": (),
    "users": ("password_hash",),
    "api_tokens": ("secret_hash",),
}
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
PARTIAL_PREFIX = ".partial-"
MANIFEST = "manifest.json"


class BackupError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class BackupInfo:
    path: Path
    created_at: datetime
    reason: str
    files: dict[str, int]
    """File name -> size in bytes."""

    @property
    def age(self) -> timedelta:
        return utcnow() - self.created_at


# --- export --------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


def _rows(conn: Connection, table: str, skip: tuple[str, ...]) -> list[dict[str, Any]]:
    """Rows of a core table, read through its schema definition (so JSON columns come
    back as data and timestamps as datetimes)."""
    schema = state_metadata.tables[table]
    present = {c["name"] for c in inspect(conn).get_columns(table)}
    columns = [c for c in schema.columns if c.name not in skip and c.name in present]
    first = next(iter(schema.primary_key.columns), columns[0])
    result = conn.execute(select(*columns).order_by(first))
    return [{c.name: _jsonable(v) for c, v in zip(columns, row, strict=True)} for row in result]


def export_state(runtime: Runtime, *, strict: bool = True) -> dict[str, Any]:
    """Everything irreplaceable in `state.db`, as JSON-compatible data. With
    `strict=False` (backups before a migration), tables or module exporters that don't
    fit the current schema are skipped and listed under `errors` instead of failing."""
    errors: list[str] = []
    core: dict[str, list[dict[str, Any]]] = {}
    modules: dict[str, Any] = {}
    with runtime.dbs.state.read() as conn:
        existing = set(inspect(conn).get_table_names())
        for table, skip in CORE_EXPORT_TABLES.items():
            if table not in existing:
                if strict:
                    raise BackupError(f"table {table} is missing; run `steward db upgrade`")
                errors.append(f"core.{table}: missing")
                continue
            core[table] = _rows(conn, table, skip)
        for owner, exporter in runtime.registrations.exporters().items():
            try:
                modules[owner] = {
                    table: [{k: _jsonable(v) for k, v in row.items()} for row in rows]
                    for table, rows in exporter(conn).items()
                }
            except Exception as exc:
                if strict:
                    raise BackupError(f"module {owner}'s exporter failed: {exc}") from exc
                errors.append(f"{owner}: {type(exc).__name__}")
    data: dict[str, Any] = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "app_version": steward.__version__,
        "exported_at": utcnow().isoformat(),
        "core": core,
        "modules": modules,
    }
    if errors:
        data["errors"] = errors
    return data


# --- backups -------------------------------------------------------------------------


def _copy_database(db: Database, destination: Path) -> None:
    if not db.exists():
        raise BackupError(f"{db.name} database {db.path} doesn't exist")
    source = sqlite3.connect(f"file:{db.path}?mode=ro", uri=True, timeout=30)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def backup(runtime: Runtime, *, reason: str = "manual", keep: int | None = None) -> BackupInfo:
    """Write a backup and prune old ones. Raises `BackupError`."""
    config = runtime.config.steward
    directory = config.backup_dir
    directory.mkdir(parents=True, exist_ok=True)
    created = utcnow()
    name = created.strftime(TIMESTAMP_FORMAT)
    final = directory / name
    n = 2
    while final.exists():
        final = directory / f"{name}-{n}"
        n += 1
    partial = directory / f"{PARTIAL_PREFIX}{final.name}"
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(mode=0o700)
    try:
        _copy_database(runtime.dbs.state, partial / "state.db")
        _copy_database(runtime.dbs.index, partial / "index.db")
        export = export_state(runtime, strict=reason != "pre-migrate")
        (partial / "state.json").write_text(json.dumps(export, indent=1, sort_keys=True))
        files = {p.name: p.stat().st_size for p in sorted(partial.iterdir())}
        manifest = {
            "format": "steward-backup",
            "version": 1,
            "created_at": created.isoformat(),
            "reason": reason,
            "app_version": steward.__version__,
            "migrations": {
                name: {s.owner: s.current for s in runtime_status(runtime, name)}
                for name in ("state", "index")
            },
            "files": {
                n: {"size": size, "sha256": _sha256(partial / n)} for n, size in files.items()
            },
        }
        (partial / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True))
        for path in partial.iterdir():
            path.chmod(0o600)
        os.rename(partial, final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    info = BackupInfo(final, created, reason, files)
    removed = prune(directory, keep if keep is not None else config.backup.keep)
    log.info(
        "backup written",
        extra={"fields": {"path": str(final), "reason": reason, "pruned": len(removed)}},
    )
    return info


def runtime_status(runtime: Runtime, database: str) -> list[Any]:
    from steward.core import reindex
    from steward.core.db import migrate

    db = runtime.database(database)  # type: ignore[arg-type]
    if not db.exists():
        return []
    with reindex.naming_recovery(database):
        return migrate.status(db, runtime.migration_owners(database))  # type: ignore[arg-type]


def list_backups(directory: Path) -> list[BackupInfo]:
    """Complete backups, newest first."""
    if not directory.is_dir():
        return []
    found = []
    for path in directory.iterdir():
        manifest = path / MANIFEST
        if path.name.startswith(PARTIAL_PREFIX) or not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text())
            created = datetime.fromisoformat(data["created_at"])
        except (ValueError, KeyError, OSError):
            continue
        sizes = {n: int(f["size"]) for n, f in data.get("files", {}).items()}
        found.append(BackupInfo(path, created, str(data.get("reason", "")), sizes))
    return sorted(found, key=lambda b: b.created_at, reverse=True)


def latest_backup(directory: Path) -> BackupInfo | None:
    backups = list_backups(directory)
    return backups[0] if backups else None


def prune(directory: Path, keep: int) -> list[Path]:
    """Delete complete backups beyond the newest `keep`, and stale partial ones."""
    removed = []
    for old in list_backups(directory)[keep:]:
        shutil.rmtree(old.path)
        removed.append(old.path)
    cutoff = utcnow() - timedelta(days=1)
    for path in directory.glob(f"{PARTIAL_PREFIX}*"):
        if datetime.fromtimestamp(path.stat().st_mtime, tz=UTC) < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path)
    return removed


def before_migrate(runtime: Runtime) -> BackupInfo | None:
    """Migrations never run on existing databases without a recent backup: if a
    migration is pending and no backup is newer than `backup.max_age_before_migrate_hours`,
    take one now. Returns the backup taken, or None if none was needed."""
    if not runtime.dbs.state.exists():
        return None  # a fresh install has nothing to lose
    pending = [
        s
        for name in ("state", "index")
        for s in runtime_status(runtime, name)
        if not s.up_to_date and s.current is not None
    ]
    if not pending:
        return None
    max_age = timedelta(hours=runtime.config.steward.backup.max_age_before_migrate_hours)
    latest = latest_backup(runtime.config.steward.backup_dir)
    if latest is not None and latest.age <= max_age:
        return None
    try:
        return backup(runtime, reason="pre-migrate")
    except (BackupError, OSError, sqlite3.Error) as exc:
        raise BackupError(
            f"a migration is pending and there's no backup from the last "
            f"{max_age.total_seconds() / 3600:.0f}h, and taking one failed: {exc}"
        ) from exc
