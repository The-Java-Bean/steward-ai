# 0006. Behavior comes from folder types, never folder names

- Status: Accepted
- Date: 2026-10-27

## Context
Everyone organizes files differently: `Documents/Photos/Private`, numbered folders,
one folder per year, several disks. steward-ai has to work for all of them from a
fresh clone, without code that recognizes particular folder names or positions.

## Decision
- A **library** is a root folder steward-ai manages. Paths are stored as
  `(library_id, relative path)`, never absolute, so mount points can change.
- A **folder type** is a named bundle of settings: `description` (also shown to the
  AI), `index` (`full`/`metadata`/`none`), `hash` (`full`/`sampled`), `stages`,
  `sensitivity`, `locked`, `writable`, `shareable`, `triage_target`, `default_tags`,
  `fields`. Built-in types (`general`, `inbox`, `documents`, `media`, `notes`,
  `projects`, `archive`, `software`, `games`, `private`, `trash`, `app-managed`,
  `ignored`) can be edited but not deleted. Custom types `extend` another type and
  inherit whatever they don't set.
- Folders are **mapped** to types. Mappings inherit down the tree, the most specific
  wins, and a library root defaults to `general`. Folders have identities, so a
  mapping follows a folder when it's renamed or moved.
- **Roles** point at single folders: `trash`, `edit_backups`, default `inbox`,
  `digest_output`. If none is chosen, hidden defaults are created inside the first
  library, on the same filesystem.
- Mappings live in `state.db` (editable in the UI without a restart), are exported in
  `state.json`, and can be imported from YAML for reproducible setups.
- Locking and permanence are settings: any type or folder can be locked, and a lock
  can be marked permanent (removable only from the host's command line). Nothing is
  hardcoded; the setup wizard asks the owner to choose a private folder.
- **No code refers to a folder by name or position.** A test scans module code for
  path literals, and the integration suite runs against two different fixture layouts.

## Consequences
- The same install works for any layout; only the mapping differs.
- Renaming or reorganizing folders never silently changes behavior.
- Every feature has to express what it needs as a folder-type setting or a role, which
  takes more design than checking a name.
- The setup wizard has to suggest types (name heuristics, file kinds, optionally a
  model) so a new user isn't left mapping everything by hand.
