# 0016. Backups, exports and migrations

- Status: Accepted
- Date: 2026-10-28

## Context
`state.db` holds what can't be rebuilt (folder settings, file identities, the journal,
accounts). Migrations change it in place. Plan §16.4 asks for `steward backup`,
`steward export`, and no migration without a recent backup.

## Decision
- `steward backup` writes `<backup_dir>/<UTC timestamp>/` with `state.db` and
  `index.db` copied through SQLite's online backup API (safe while running),
  `state.json` (the export) and `manifest.json` (app version, migration revisions, sizes
  and SHA-256). It's written under a `.partial-` name and renamed when complete, files
  are `0600`, and only the newest `backup.keep` are kept.
- `steward export` (and `state.json`) is plain JSON of the core tables that can't be
  rebuilt plus each module's `Registry.exporter`. It leaves out jobs, events, sessions,
  login attempts, password hashes and token secrets.
- A `core.backup` schedule runs daily at 03:30; owners whose backup tool runs
  `steward backup` as a hook can turn it off in `pipeline.yaml`. The health check warns
  when there's no backup or the last is over two days old.
- Before migrating **existing** databases with a pending revision, `steward api` and
  `steward db upgrade` take a backup unless one is younger than
  `backup.max_age_before_migrate_hours` (24). If that backup fails, they don't migrate.

## Consequences
- Backups need space for one more copy of both databases each night.
- Restoring is copying the two database files back while steward-ai is stopped.
