# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text
from support import make_module, make_runtime

from steward.core.db import migrate
from steward.core.db.schema import index_metadata, state_metadata
from steward.sdk import Registry

REVISION = """
from alembic import op
import sqlalchemy as sa

revision = "{rev}"
down_revision = {down!r}
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("{table}", sa.Column("id", sa.Integer, primary_key=True))


def downgrade():
    op.drop_table("{table}")
"""


def module_with_tables(name: str, versions: Path, *tables: str) -> type:
    versions.mkdir(parents=True, exist_ok=True)
    down = None
    for i, table in enumerate(tables, start=1):
        rev = f"{i:04d}"
        (versions / f"{rev}_{table}.py").write_text(
            REVISION.format(rev=rev, down=down, table=table)
        )
        down = rev

    def register(r: Registry) -> None:
        r.migrations(versions, database="state")

    return make_module(name, register=register)


def tables(runtime_db: object) -> set[str]:
    with runtime_db.read() as conn:  # type: ignore[attr-defined]
        return set(inspect(conn).get_table_names())


def test_fresh_upgrade_creates_core_tables_and_is_idempotent(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, upgrade=False)
    assert runtime.upgrade_databases() == {"state": ["core"], "index": ["core"]}
    assert runtime.upgrade_databases() == {"state": [], "index": []}
    assert {"jobs", "events", "event_cursors", "schedules"} <= tables(runtime.dbs.state)
    runtime.ensure_databases_current()


def test_databases_use_wal_and_foreign_keys(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    with runtime.dbs.state.read() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


@pytest.mark.parametrize("database", ["state", "index"])
def test_migrations_match_schema_definitions(tmp_path: Path, database: str) -> None:
    runtime = make_runtime(tmp_path)
    metadata = state_metadata if database == "state" else index_metadata
    db = runtime.dbs.state if database == "state" else runtime.dbs.index
    with db.read() as conn:
        ctx = MigrationContext.configure(
            conn,
            opts={
                "include_name": lambda name, type_, parent: (
                    not str(name).startswith(("alembic_version", "chunks_fts"))
                )
            },
        )
        diff = compare_metadata(ctx, metadata)
    assert diff == []


def test_missing_database_is_reported(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, upgrade=False)
    with pytest.raises(migrate.MigrationError, match="steward db upgrade"):
        runtime.ensure_databases_current()


def test_module_migrations_run_after_core_with_their_own_version_table(tmp_path: Path) -> None:
    module = module_with_tables("notes", tmp_path / "notes_versions", "notes_items", "notes_links")
    runtime = make_runtime(tmp_path, [module])
    assert {"notes_items", "notes_links", "alembic_version_notes"} <= tables(runtime.dbs.state)
    statuses = migrate.status(runtime.dbs.state, runtime.migration_owners("state"))
    assert [s.owner for s in statuses] == ["core", "notes"]
    assert all(s.up_to_date for s in statuses)
    assert statuses[1].current == "0002"


def test_module_tables_must_use_the_module_prefix(tmp_path: Path) -> None:
    module = module_with_tables("notes", tmp_path / "v", "notes_ok", "stuff")
    runtime = make_runtime(tmp_path, [module], upgrade=False)
    with pytest.raises(migrate.MigrationError, match="not named 'notes_\\*': stuff"):
        runtime.upgrade_databases()
    assert not {"stuff", "notes_ok"} & tables(runtime.dbs.state)


def test_disabling_a_module_does_not_block_other_migrations(tmp_path: Path) -> None:
    notes = module_with_tables("notes", tmp_path / "notes_v", "notes_items")
    make_runtime(tmp_path / "a", [notes]).close()
    # Same data dir, module now absent: core still upgrades, module tables stay.
    data_dir = str(tmp_path / "a" / "data")
    runtime = make_runtime(tmp_path / "b", steward={"data_dir": data_dir})
    runtime.ensure_databases_current()
    assert "notes_items" in tables(runtime.dbs.state)


def test_module_migrations_downgrade(tmp_path: Path) -> None:
    versions = tmp_path / "v"
    runtime = make_runtime(tmp_path, [module_with_tables("notes", versions, "notes_a", "notes_b")])
    migrate.downgrade(runtime.dbs.state, "notes", versions, "0001")
    assert "notes_b" not in tables(runtime.dbs.state)
    migrate.downgrade(runtime.dbs.state, "notes", versions, "base")
    assert not {"notes_a", "notes_b"} & tables(runtime.dbs.state)
    assert "jobs" in tables(runtime.dbs.state)
