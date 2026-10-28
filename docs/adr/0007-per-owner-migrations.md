# 0007. Each migration owner has its own Alembic history

- Status: Accepted
- Date: 2026-10-28

## Context
The core and every module that stores data need schema migrations in `state.db` and
sometimes `index.db` (ADR 0001). Modules can be disabled and re-enabled, installed and
removed. Alembic's usual model is one revision tree per database with one version
table. If a module's revisions live in that shared tree and the module is later
disabled (its revision files no longer loaded), Alembic can't find revisions recorded
in the version table and refuses to run, which blocks every other upgrade.

## Decision
- Every **owner** (`core`, or a module name) has its own directory of revision files
  and its own version table, `alembic_version_<owner>`, in each database it uses. The
  histories are independent: no branches, no cross-owner `down_revision`s.
- One shared Alembic environment (`steward/core/db/alembic/env.py`) runs every owner;
  the runner passes the connection and the owner's version table in.
- Upgrades run core first, then enabled modules in dependency order, each owner in its
  own `BEGIN IMMEDIATE` transaction. A module's migrations may only create tables
  named `<module>_*`; a violation rolls that owner's upgrade back and fails.
- Modules register their revision directory with
  `Registry.migrations(path, database="state" | "index")`.
- `steward db upgrade` applies migrations; `steward db status` shows each owner's
  revision; the worker refuses to start unless every owner is at its head. (Running
  migrations on API start, and refusing to migrate without a recent backup, come with
  the API and backup work.)
- Core tables are also declared as SQLAlchemy Core metadata
  (`steward/core/db/schema.py`), and a test checks that migrations and metadata agree.

## Consequences
- Disabling a module never blocks other upgrades; its tables and version row stay,
  and re-enabling it continues from its recorded revision.
- A module's migrations can rely on core tables (core always migrates first) and on
  tables of the modules it `requires` (they migrate earlier), but on no others.
- Removing a module's data entirely is an explicit downgrade to `base` for that owner.
