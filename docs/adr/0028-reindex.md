# 0028. Rebuilding the index in place, with the scanner restoring what's lost

- Status: Accepted
- Date: 2026-10-30

## Context
`index.db` is meant to be disposable, but deleting it wasn't enough. `files.content_hash`
lives in `state.db` and `blobs` in `index.db`, and a scan skips unchanged files, so after
losing `index.db` nothing was ever indexed again, silently. The same goes for restoring an
older `index.db`. Replacing the file under a running API and worker isn't safe either:
writes go to the old file, and when the old connections close SQLite unlinks `-wal` and
`-shm` by name, which can damage the new one. And `photos.sidecars`, seeing photos with no
AI tags or description mid-rebuild, would strip what it wrote from every sidecar, for good
if no model is mapped.

## Decision
- **The scanner repairs.** A file whose hash has no blob is read again even if unchanged.
  Same hash: the blob is restored and a new event `file.reindexed` goes to the planner.
  Other hash: the usual `file.changed`. One readable copy of a content is enough, excluded
  files stay unread, and an unreadable copy doesn't stop the next one being tried. So any
  full scan repairs a lost or older `index.db`, not only the command.
- **`steward reindex`** says what will happen (stages computed again, stages that won't
  run because their model isn't mapped), asks (`--yes`), takes a `pre-reindex` backup
  (`--no-backup`), then in one transaction sets `index.rebuild` in `settings_kv` and queues
  `core.index.rebuild`.
- **The job empties `index.db` in place, in one transaction.** Schema and version tables
  stay, module tables are included (also of modules turned off), triggers are dropped and
  recreated around it, children go before parents. Readers see the old index until it
  commits. A stage caught in the middle fails on the foreign key and retries, or leaves a
  result the planner recomputes. It then queues a full scan of each library under its own
  dedupe key (a scan already running may have passed files before the reset) and a full
  pass.
- **Nothing else starts while it empties.** It took 108–167 s for 500,000 chunks with
  vectors on an HDD, and a writer waits 10 s for the lock. `index.rebuild.emptying` makes
  workers claim only the rebuild job (`reindex.claimable`); jobs already running either
  finish first or retry. If the rebuild job is gone (killed and out of attempts), the mark
  stops counting, so a crash can't stop the queue for good. A thread renews the job's lease
  meanwhile. Every write to `index.db` is in a job except a foreground `steward scan`,
  which fails with "database is locked" if run during that window.
- **The rebuild ends by itself:** worker housekeeping clears `index.rebuild` when no scan,
  planning or stage job is queued or running and no planner event is pending. Jobs waiting
  for a model count, so it lasts as long as the model is away. `--status` and the health
  check `core.index` show it.
- **Sidecars wait.** `LibraryAccess.rebuilding()`; `photos.sidecars` returns without
  touching anything or storing its revision while it's true.
- **A missing file:** stop everything, delete `index.db*`, run `steward reindex`, start.
  With `index.db` gone and `-wal`/`-shm` still there the command refuses: something is
  running, or a stale WAL would be replayed into the new file. A worker starting with
  hashed files and no blobs at all requests the rebuild itself. A damaged file stops every
  command at startup with that recovery (`naming_recovery`), or fails the job permanently if
  the damage shows up there; "database is locked" is retried instead.

## Consequences
- With a fake model a rebuild is exact. With a real one, on a 69-file sample: search and
  sidecars came back the same, and one borderline AI tag changed (see ADR 0027).
- Results of stages with no mapped model don't come back until one is mapped; the command
  lists them before anything happens.
- The queue is paused for a few minutes on a big index. Running a VACUUM afterwards isn't
  done: the freed pages are reused by the rebuild.
- Every scan does one extra `IN` query per batch of hashes to find lost blobs.
- A module that acts on what the index doesn't say must check `rebuilding()`.

## Alternatives considered
- Delete and recreate the file in the job: unsafe with open connections (above).
- Batched deletes to keep lock times short: readers would see a half-empty index for
  minutes and a crash would leave one, with no gain since nothing else should run anyway.
- `DROP TABLE` + migrations: module tables of disabled modules wouldn't come back, and the
  version tables would have to be rewritten.
