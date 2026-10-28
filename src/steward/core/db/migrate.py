# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Schema migrations for core and modules.

Every owner (`core`, or a module name) has its own directory of Alembic revisions and
its own version table (`alembic_version_<owner>`) in each database it uses. Owners are
independent: a disabled module's applied revisions never block anyone else's upgrade,
and re-enabling it continues from where it was (docs/adr/0007).

Core migrates first, then modules in dependency order. A module's migration may only
create tables named `<module>_*`; anything else rolls the upgrade back.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect

from steward.core.db.engine import Database

ALEMBIC_DIR = Path(__file__).parent / "alembic"
CORE_VERSIONS = {
    "state": Path(__file__).parent / "versions" / "state",
    "index": Path(__file__).parent / "versions" / "index",
}


class MigrationError(Exception):
    """A migration failed, broke a rule, or the schema isn't at the expected revision."""


@dataclass(frozen=True, slots=True)
class OwnerStatus:
    database: str
    owner: str
    current: str | None
    head: str | None

    @property
    def up_to_date(self) -> bool:
        return self.current == self.head


def version_table(owner: str) -> str:
    return f"alembic_version_{owner}"


def upgrade(db: Database, owners: Mapping[str, Path]) -> list[str]:
    """Bring every owner in `owners` (in order) to its head. Returns owners that changed."""
    db.path.parent.mkdir(parents=True, exist_ok=True)
    changed = []
    for owner, versions in owners.items():
        with db.write() as conn:
            before_rev = _current(conn, owner)
            before_tables = set(inspect(conn).get_table_names())
            command.upgrade(_alembic_config(conn, owner, versions), "head")
            after_tables = set(inspect(conn).get_table_names())
            _check_table_names(owner, after_tables - before_tables)
            if _current(conn, owner) != before_rev:
                changed.append(owner)
    return changed


def downgrade(db: Database, owner: str, versions: Path, revision: str) -> None:
    """Downgrade one owner to `revision` (`base` removes all of its tables)."""
    with db.write() as conn:
        command.downgrade(_alembic_config(conn, owner, versions), revision)


def status(db: Database, owners: Mapping[str, Path]) -> list[OwnerStatus]:
    result = []
    with db.read() as conn:
        for owner, versions in owners.items():
            head = ScriptDirectory.from_config(_alembic_config(conn, owner, versions))
            result.append(
                OwnerStatus(db.name, owner, _current(conn, owner), head.get_current_head())
            )
    return result


def ensure_current(db: Database, owners: Mapping[str, Path]) -> None:
    """Raise unless the database exists and every owner is at its head."""
    if not db.exists():
        raise MigrationError(
            f"{db.name} database {db.path} does not exist; run `steward db upgrade`"
        )
    behind = [s for s in status(db, owners) if not s.up_to_date]
    if behind:
        details = ", ".join(
            f"{s.owner} at {s.current or 'nothing'} (head {s.head})" for s in behind
        )
        raise MigrationError(f"{db.name} database needs `steward db upgrade`: {details}")


def _alembic_config(conn: Connection, owner: str, versions: Path) -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("version_locations", str(versions))
    cfg.set_main_option("path_separator", "os")
    cfg.attributes["connection"] = conn
    cfg.attributes["version_table"] = version_table(owner)
    return cfg


def _current(conn: Connection, owner: str) -> str | None:
    ctx = MigrationContext.configure(conn, opts={"version_table": version_table(owner)})
    return ctx.get_current_revision()


def _check_table_names(owner: str, created: set[str]) -> None:
    created = {t for t in created if not t.startswith("alembic_version_")}
    if owner == "core":
        return
    bad = sorted(t for t in created if not t.startswith(f"{owner}_"))
    if bad:
        raise MigrationError(
            f"module {owner!r} migrations created tables not named '{owner}_*': {', '.join(bad)}"
        )
