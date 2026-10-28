# 0011. Stage framework: a graph of stages, planned from current state

- Status: Accepted
- Date: 2026-10-28

## Context
After discovery, each file's content goes through processing stages (text extraction,
OCR, chunking, embeddings, EXIF, captions…) that modules provide. Stages depend on each
other's output, should run once per unique content rather than per path, must re-run
when they change, and must respect folder types (a `media` folder adds captions and
skips text extraction; a metadata-only folder gets no content stages). A first index of
a large library produces one event per file (ADR 0010), so replaying events is a poor
basis for deciding what to run.

## Decision
- **SDK:** `Registry.stage(name, run, version=, produces=, requires=, lane=,
  applies_to=, default=, index=)`. `requires`/`produces` are artifact kinds (`text`,
  `text:ocr`, `embedding:<space>`). `run(ctx: StageContext)` reads the file through
  `ctx.open()` (read-only, no symlinks, refuses a file that changed since it was
  scanned) and writes its module's `index.db` tables through `ctx.index_write()`;
  it returns `StageResult` (`done` or `skipped` with a reason) or raises.
  `applies_to(blob, file, folder)` is a cheap, pure check.
- **Names:** stages are registered as `<module>.<name>`; folder types may refer to a
  stage by its full or short name (`ocr` matches `documents.ocr`).
- **Root:** discovery's hashing is declared as stage `core.hash` (produces `hash`,
  `mime`). Discovery computes it itself because it decides file identity; it's never
  queued as a job.
- **Graph:** built at startup from the enabled stages. Duplicate producers, required
  kinds nobody produces, and cycles stop startup (`StageGraphError`, a
  `ModuleLoadError`).
- **Results** are keyed by content hash in `index.db` `stage_runs` (version, status
  `done`/`skipped`/`failed`, error or skip reason, model, prompt version, time). A
  result is *current* when it was made by the stage's current version after its inputs
  were last recomputed; *usable* when current and `done`.
- **Wanted:** a stage is wanted for a content when some present file with that content
  lies in a folder whose type enables it (`default` or `stages.add`, minus
  `stages.remove`; `index: none` disables all, `index: metadata` allows only stages
  declared `index="metadata"`) and `applies_to` is true.
- **Planner:** queues `core.stage.<stage>` jobs (dedupe `stage:<stage>:<hash>`) for
  wanted stages whose inputs are usable and whose own result isn't current. It reads
  current state (`files`, `blobs`, `stage_runs`), never event history, so re-planning is
  always safe. Failed results at the current version are not re-planned (the job already
  retried; `steward jobs retry` or a version bump runs it again).
- **Triggers:** the worker's housekeeping queues `core.stages.plan` when new `file.*`
  events exist past the planner's own cursor (an `event_cursors` row); that job plans
  the named files. Folder and folder-type changes queue `core.stages.full`, a pass over
  every present content, which also runs daily (04:20) and at worker start when the
  graph's signature (names, versions, shape) differs from the last full pass's
  (`index.db` `pipeline_meta`). A finished stage plans its content again so dependents
  follow at once.
- **Files changing under a stage:** if the file no longer matches discovery's record
  before or after `run`, the result is dropped and a scan of that path is queued.

## Consequences
- Moving or copying a file costs nothing; identical copies are processed once.
- Raising a stage's version re-runs it and, as their inputs are recomputed, everything
  downstream; nothing else.
- `applies_to` changes aren't detected; module authors bump `version` when they matter.
- Stage writes and the `stage_runs` row are separate transactions, so a stage can run
  twice for the same content; stage writes must be idempotent upserts.
- A stage's result reflects one file holding the content; with sampled hashes two
  different files could in theory share a hash (accepted in ADR 0010).
- A folder-type change during a running full pass is only seen by later batches until
  the next full pass.
- `blob.stage_completed` events aren't emitted yet (dependents are planned directly);
  add them when a subscriber needs them.
