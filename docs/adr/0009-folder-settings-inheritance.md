# 0009. Folder settings: types inherit, restrictions carry down

- Status: Accepted
- Date: 2026-10-28
- Refines: [0006](0006-folder-types.md)

## Context
ADR 0006 makes folder types the only source of folder behavior. Two questions were
left open: how a folder's settings combine with its ancestors', and how to keep a
mapping mistake (a subfolder of a private folder mapped to an ordinary type) from
exposing private files. Separately, the `trash` and `edit_backups` folders receive
copies of files from anywhere, including private folders.

## Decision
- **Type inheritance (between types):** a custom type `extends` another (default
  `general`) and inherits every setting it leaves unset. Built-in types implicitly
  extend `general`. The owner may override a built-in's settings, not its parent.
  List settings (`default_tags`, `fields`, `ignore`) replace the parent's; `stages`
  `add`/`remove` accumulate along the chain.
- **Type of a folder:** the type of its nearest mapped ancestor-or-self; an unmapped
  library root is `general`.
- **Restrictions carry down (between folders):** along the path from the library root,
  sensitivity takes the highest value, and `not writable`, `not shareable` and
  `not visible` apply to everything inside. Mapping a subfolder to a looser type never
  loosens them.
- **Locks:** a lock decision is made only where a folder is mapped: its own lock
  setting if it has one, otherwise its own type. A locked folder locks everything
  inside it, and a subfolder can't unlock itself. Unlocking a folder whose type is
  locked (e.g. `private`) unlocks its contents too, but doesn't lower sensitivity. A
  **permanent** lock is stored as an explicit lock on the folder, so changing its type
  doesn't remove it, and only `steward folders unlock --permanent` on the host lifts it.
- Locked folders are never shareable or triage targets; non-writable ones are never
  triage targets.
- **Copy-holding roles:** folders with the `trash` or `edit_backups` role, and their
  contents, are at least `local-only`, never shareable and never triage targets. Every
  role's folder must be writable; `inbox` and `digest_output` can't be locked.
- **Identity:** folders have stable IDs, so mappings, locks and roles follow a folder
  that's renamed or moved; moves are recorded in `path_history`.
- **Seeding:** `libraries.yaml` and `folders.yaml` are imported with
  `steward folders import`. Importing only adds and updates; it never removes and never
  lifts a permanent lock. The database is the source of truth.

## Consequences
- Privacy and lock settings fail closed under any mapping, which the policy tests
  check on two different layouts.
- To make a subfolder of a private folder shareable, the owner has to restructure or
  change the parent; there's no per-subfolder escape hatch, deliberately.
- Open: files moved to the trash from a locked folder must stay hidden outside the
  Locked Folder. That has to be solved in the executor (Phase 1) and the Locked Folder
  view (Phase 2), for example by recording the original lock with each trashed item.
