# 0010. Discovery and file identity

- Status: Accepted
- Date: 2026-10-28

## Context
steward-ai has to notice new, changed, renamed, moved and deleted files in libraries
it only reads, keep each file's identity (its tags, notes and history) through renames
and edits, and keep folder mappings attached when a folder is renamed outside
steward-ai. It runs next to other programs on shared, often spinning, disks, and a
library's disk may be unmounted at any time.

## Decision
- **A full scan is the source of truth**; the watcher (watchdog/inotify on each library
  root) only makes it fast. Full scans run on worker start and every 6 hours
  (`core.reconcile` → one `core.scan` job per library); watcher events become
  `core.scan` jobs for just the changed paths after a 10 s debounce.
- **Hashes are BLAKE3**, `b3:<hex>` (whole file) or `b3s:<hex>` (size + first, middle
  and last MiB) when the folder's type says `hash: sampled`. Files of 3 MiB or less
  always get a full hash. Files are only re-hashed when size, mtime or inode changed.
- **File identity** (`files.file_id`, a UUID), in order:
  1. same path → same file (a new hash is an edit, `file.changed`);
  2. a vanished file in the same library with the same inode, size and mtime → a
     rename (no re-hash);
  3. a vanished file anywhere with the same content hash (sampled: plus size and
     mtime) → a move, preferring the same file name;
  4. otherwise a new file. Identical copies are separate files sharing a blob.
  "Vanished" = not at its recorded path, and either still marked present or missing
  for less than `move_window_hours` (72). Paths that exist are scanned before paths
  that don't, so a move is matched before its source is marked missing.
- **Folder identity:** a folder with no identity at its path that has the inode of a
  vanished folder in the same library is that folder renamed or moved
  (`store.move_folder`: mapping, lock, roles, subfolders and files follow). If the old
  folder had files, at least one must be inside the new one, so a reused inode can't
  pass a mapping (or a lock) to an unrelated folder.
- **Nothing is deleted.** Gone files and folders get `present = 0` and
  `missing_since`; `file.missing` is emitted.
- **Safety:** a scan refuses to run if the library root is missing, or empty while
  files were indexed there (an unmounted disk), unless explicitly allowed
  (`steward scan --allow-empty`). Folders that can't be read are not counted as missing.
- **Skipped:** symlinks (never followed), special files, names that aren't valid
  UTF-8, temporary-file patterns (`pipeline.yaml` `discovery.ignore_patterns`), a folder
  type's `ignore` list, and folders whose type has `index: none` (their known files
  become missing).
- **Paths are stored exactly as on disk** (no Unicode normalization), so they always
  lead back to the file.
- **Disk care:** the `io` lane's threads run at idle IO priority (Linux `ioprio_set`),
  concurrency 1 by default, with an optional read-rate cap.
- `blobs` and `stage_runs` (stage `hash`, version 1) live in `index.db`; `files` lives
  in `state.db`. Discovery writes only the databases, never the libraries.
- Events: `file.discovered`, `file.changed`, `file.moved`, `file.reappeared`,
  `file.missing` (payloads: IDs, paths, hash, size, MIME; never content).

## Consequences
- Renaming or moving folders and files, on the same disk or by copy-and-delete, keeps
  identities and mappings, even when the watcher missed it.
- An NFD-named folder (e.g. copied from macOS) doesn't match a mapping typed in NFC.
  Rare; revisit if it happens.
- Every file event goes through the outbox; a first index of a large library writes one
  event per file. Fine for SQLite, but the stage planner (Phase 1/3) should work from
  `stage_runs` rather than replay events.
- Moving a folder records one `path_history` row per folder; its files' old paths
  follow from the folder's.
- Scans of overlapping paths running at the same time (io concurrency > 1) can conflict
  on a new row; the job fails and is retried.
