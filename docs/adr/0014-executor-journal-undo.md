# 0014. The executor: action types, the journal and undo

- Status: Accepted
- Date: 2026-10-28

## Context
ADR 0005 says one executor makes every file change, each change is an `ActionType`,
and each is journaled with an inverse. This record fixes how that works in code: the
action protocol, how paths are checked, what the journal holds, and how undo stays safe
when files change underneath it.

## Decision
- **Action types** (`steward.sdk.actions`) have pydantic `params`, `plan(params, view)`
  (read-only; returns a `Plan` with preconditions, `reads`, `writes` and data for
  `apply`), `render(plan)` (a summary for people), `apply(plan, fs)` and
  `inverse(plan, applied, view)` (the params of an action that undoes it, or `None`).
  Every path in params is a `LibraryPath` (`library:relative/path`). Core types:
  `file.mkdir`, `file.mkdirs`, `file.rmdir`, `file.move`, `file.rename`, `file.delete`,
  `file.create`, `file.edit`, `folder.lock`, `folder.unlock`.
- **The guarded filesystem** (`core.executor.guarded`) is the only write path. It opens
  every component with `O_NOFOLLOW` relative to the library root's descriptor (no
  symlinks anywhere on the path), rejects NUL, `\`, control characters, `.`/`..`/empty
  segments and names over 255 bytes, NFC-normalizes new names, and refuses any write
  outside the plan's `writes` or into a non-writable folder. Renames use
  `renameat2(RENAME_NOREPLACE)`; new files go to a temp file, `fsync`, then `link`, so
  nothing is ever overwritten.
- **Running an action**, under one process-wide write lock: re-check the paths, decide
  sensitivity (the type's flag, or any path inside a locked folder), refuse a sensitive
  action without `confirmed`, re-check the preconditions, apply, build the inverse, then
  in one `state.db` transaction: folder setting changes, file and folder identities
  following moves, the journal entry, the `action.applied` event, and rescans of the
  touched paths. A failure emits `action.failed`.
- **The journal** (`journal` table) stores the action, params, plan, result, inverse,
  summary, actor, sensitivity, links between an entry and its undo, and the time.
- **Undo** runs the inverse as a normal action whose preconditions are the original's
  post-state, so it's refused if anything changed since. Undoing twice is refused.
- **Delete** is a move into `<trash>/<YYYY-MM-DD>/<path>`: the `trash` role folder if it
  is in the same library, otherwise that library's own `.steward/trash` (moves across
  libraries fail with `EXDEV`), created and typed `trash` on first use. Files from a
  locked folder go to a locked `trash/locked` subfolder, so the trash never exposes
  them (plan §21 open question 2).
- **Edits** keep a copy in the `edit_backups` role folder,
  `YYYY/MM/<file_id>/<timestamp>-<name>`, before writing through a temp file.
- Actions run synchronously in the request (UI, API, CLI) under the write lock. The
  `executor` job lane stays for queued actions (approved proposals, later).
- **A safety harness** (`tests/executor/test_action_safety.py`) runs every registered
  action's examples through undo round trips, drift, hostile paths, symlinked folders,
  collisions, read-only folders and Unicode names.

## Consequences
- New file operations need only an `ActionType` with examples; the harness tests them.
- Trash and edit backups grow until the owner empties them; steward-ai never does.
- Synchronous actions keep the UI simple, but a slow disk blocks that request.
