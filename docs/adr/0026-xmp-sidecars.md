# 0026. XMP sidecars: drafts outside the write lock, three-way merge, sidecars follow files

- Status: Accepted
- Date: 2026-10-29

## Context
Tags and descriptions should reach other photo programs without touching originals, so
they go into XMP sidecars next to the photos. Sidecars are shared with those programs,
which also write them. Only the API process may write, and a batch of sidecars competes
with people using the file manager. exiftool takes 187 ms per process in the image, and
a library can hold 100,000 photos.

## Decision
- **Which photos:** `image` files in folders whose type has the new `sidecars` setting
  (default false, true for `media`, inherited through `extends`), and only where the
  folder is writable and not locked. Writing thousands of files into a folder should
  follow from a type someone chose. Hidden and excluded files never reach the job.
- **exiftool runs before the write lock.** The job `photos.sidecars` (`executor` lane,
  every 15 minutes) copies a batch's sidecars to a private temp folder under fixed names,
  so no file name reaches a command line, reads them with one exiftool process and
  edits them with another (`-@` argument file, `-execute`). The action only checks and
  writes the finished bytes. Under the lock a sidecar costs a write, not a process.
- **Validation:** a sidecar reaches exiftool only if it's small, looks like XMP and has no
  `DOCTYPE` or `ENTITY`. The API process has the libraries read-write, and exiftool
  chooses parsers by what it finds, so a file merely named `.xmp` must not get through.
- **`photos.sidecar_sync`** writes one batch for one folder (50 at most). Content, hashes
  and records are `Plan.transient`; the journal keeps the folder and the count, so a run
  is one line per folder and no description text is stored. It checks each sidecar's hash
  right before writing and skips drifted ones instead of failing the batch; the next
  run merges again. There's no undo: the next run would write the same thing.
- **Three-way merge** against `photos_sidecars` (per `file_id`: last wanted keywords,
  which of them steward-ai added itself, a hash of the description, whether it's ours):
  new keywords are added, keywords no longer wanted are removed only if ours, and a
  keyword another program removed stays removed. A description is written only over
  nothing or over our own. `dc:subject` gets every level of a tag, and
  `lr:hierarchicalSubject` the path (`finance|receipt`).
- **Sidecars move with their files, in the core.** `file.move`, `file.rename` and
  `file.delete` of a file also move `<name>.xmp`, whoever wrote it, in the same action;
  undo moves both back. Otherwise a photo moved into a locked folder would leave its
  description and tags in the open. A sidecar at the destination refuses the move.
- **`.xmp` is `application/rdf+xml`** (kind `other`): no stage reads it. A sidecar next
  to its file isn't listed in the file manager or found by name, and share links never
  serve sidecars: sharing a folder of photos doesn't hand out AI tags and descriptions.
- **SDK:** `JobContext.library` (`revision()`, `files()`, `read()`), which never returns
  anything locked, hidden or excluded, and `JobContext.actions` (only on the `executor`
  lane; runs as `module:<name>`, never confirmed, so sensitive and locked actions are refused).
- **Skipping idle runs:** comparing every photo costs about 18 s of CPU per 100,000
  (measured; 7,500 folders, 2 tags each), in the API process. `library.revision()` (the
  newest event and stage result) lets the job skip the whole pass when nothing changed.
  Writing sidecars changes it, so one pass with nothing to do follows every write.

## Consequences
- Other programs' sidecars are safe, and a whole run costs one exiftool process per batch.
- Exiftool is needed: without it no sidecars are written (no fallback XMP writer).
- A sidecar deleted or edited by hand isn't noticed until the photo's tags or description
  change; the job compares what steward-ai wants with what it recorded, not with the
  file. A hand-deleted sidecar is rewritten in full then.
- Moves outside steward-ai aren't seen as moves of the sidecar: the next run writes a new
  sidecar and the old one stays. If the photo went into a locked folder that way, the old
  sidecar stays in the open.
- Removing a photo for good (emptying the trash, in another tool) leaves
  `photos_sidecars` rows behind; they're small and harmless.
- Adobe-style `IMG_0001.xmp` next to `IMG_0001.jpg` isn't recognized: pairs such as
  `.jpg` + `.cr2` make that name ambiguous, so a second sidecar `IMG_0001.jpg.xmp` appears.
