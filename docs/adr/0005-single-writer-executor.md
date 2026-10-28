# 0005. A single executor is the only code that writes files

- Status: Accepted
- Date: 2026-10-27

## Context
steward-ai parses untrusted files (PDFs, images, office documents) and lets an AI
propose changes to a person's files. A parser bug or a bad proposal must not be able
to destroy data, and every change must be undoable.

## Decision
- Two processes from one image: `steward api` (web UI, API, agent, and the
  **executor lane**) and `steward worker` (discovery, queue, pipeline, events). The
  worker mounts libraries **read-only**.
- Every file change is a registered **`ActionType`** with `plan`, `render`, `apply` and
  `inverse`. Only the executor runs `apply`, through a guarded filesystem API; the SDK
  offers modules no other way to write.
- **Path rules:** writes only inside libraries and never into non-writable folder types;
  symlinks resolved and re-checked; names NFC-normalized and checked for `/`, NUL, `..`
  and over-long components. Preconditions are re-checked right before applying, and
  drift fails the action.
- **Safe operations:** moves are atomic `rename(2)` and never overwrite; edits keep a
  backup and write through a temp file, `fsync` and rename; delete means a move to the
  trash folder, which steward-ai never empties on its own; uploads stream to a temp
  file, then rename.
- Every applied action is **journaled** with its inverse, so it can be undone; undo is
  refused if the file changed since.
- Actions a person starts in the UI run directly (still journaled). Destructive or
  outward-facing actions the AI starts become **proposals** that only a human, through
  a logged-in session, can approve; no API token or agent can.
- Each library is one bind mount, because moves across separate mounts fail with
  `EXDEV` even on the same disk.

## Consequences
- A compromised worker can read but not change or delete anything.
- Every new kind of change is an `ActionType`, and the executor-safety tests (fuzzed
  paths, symlinks, collisions, apply → undo round trips) cover it automatically.
- Writes are serialized through one lane, which is fine for one owner but limits write
  throughput.
- The API process needs write access to the libraries; the worker never does.
