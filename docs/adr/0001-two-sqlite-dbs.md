# 0001. Two SQLite databases: rebuildable index, irreplaceable state

- Status: Accepted
- Date: 2026-10-27

## Context
steward-ai stores two very different kinds of data. Some of it can always be
recomputed from the files: extracted text, OCR output, chunks, embeddings, captions.
Some of it can't: the owner account, folder types and mappings, tags a person applied,
approved proposals, the action journal, shares and the tag vocabulary. Mixing them
means a reindex risks your decisions, and a backup has to carry gigabytes of
recomputable vectors. The project should also be easy to run on one small server,
with nothing extra to operate.

## Decision
- Use **SQLite in WAL mode** through SQLAlchemy 2 Core, with Alembic migrations (a
  branch for the core and one per module).
- Keep **two database files**:
  - `index.db` is rebuildable. Deleting it and running `steward reindex` must recreate
    it completely. Derived data is keyed by **content hash** (plus stage and stage
    version), not by path, so moving a file costs nothing and identical copies are
    processed once.
  - `state.db` is irreplaceable. It's backed up with SQLite's online backup API and
    exported nightly to plain JSON (`state.json`) through each module's `Exporter`.
- The job queue and the event outbox live in `state.db` too, so an event is written in
  the same transaction as the change it describes. There is no message broker.
- Database access goes through a repository layer, so Postgres (+ pgvector) can be
  added as an adapter if SQLite's limits are ever reached.

## Consequences
- Wiping and rebuilding the index is always safe; restoring a backup only needs `state.db`.
- Vectors (`sqlite-vec`) and keyword search (FTS5) live in the same file as the rest
  of the index, behind the `VectorStore` and `KeywordIndex` interfaces.
- There's no cross-database foreign key between `index.db` and `state.db`; the content
  hash is the link, and consistency is the code's job.
- One writer at a time per database file. That fits a single-owner server; the
  repository layer keeps a Postgres move possible.
- Every `state.db` table must be covered by an exporter or explicitly marked
  rebuildable; a test enforces it.
