# 0025. Fields as properties, tags as core storage

- Status: Accepted
- Date: 2026-10-29

## Context
Phase 3 chunk 5 adds facts read from documents (plan §10.2: `fields`) and tags (plan
§13.8). The plan put both in module tables (`fields_values`, `tags`/`file_tags` owned by
a `tags` module). But search filters, counts, the file manager, `steward files info`,
XMP sidecars (chunk 6), the Immich integration and the chat tools (Phase 4) all need a
file's tags and fields, and modules can't read each other's tables or the core's search
can't read a module's. Tags also mix three sources (you, the folder type's
`default_tags`, the AI), and your decisions must survive the AI changing its mind.

## Decision
- **Fields are properties** (ADR 0024) under artifact `fields`; no new table. Index
  revision `0007` adds `(key, text)` and `(key, num)` indexes on `properties`.
- **Search filters on properties**: `SearchFilters.properties` (`PropertyFilter(key, op,
  value)`, ops `: = > >= < <=`; numbers compare as numbers, otherwise as text; `:` is
  "contains" for text). Queries may hold filters: `tag:NAME`, `kind:KIND`, and
  `KEY:VALUE` / `KEY>VALUE`… for property names that exist in the index (anything else
  stays a word, so `http://x` is safe). Values may be "double quoted".
- **`fields` module** (built-in, on; requires `documents`): stage `fields.extract`
  (`mime` → `fields`; optional `text:ocr`, `text`; lane `llm-local`; role `fields`)
  sends the first `max_chars` (6000) characters with a structured-output schema of
  `doc_type` (fixed list), `title`, `date`, `sender`, `recipient`, `amount`, `currency`
  plus the folder type's `fields`, all nullable. Values are cleaned (dates to
  `YYYY[-MM[-DD]]`, currencies to ISO codes, unknown doc types to `other`, empty
  strings and "n/a" dropped). Runs for documents, and for images and text files where
  the folder type has `fields` or adds the stage. Stage version = 100 × fields version
  + prompt version.
- **Tags are core storage.** `state.db` revision `0007`: `tags` (`name`, casefolded
  `name_key` unique, `status` `active|ai-created|deprecated|alias`, `target_id` for
  aliases, `description`) and `file_tags` (`file_id`, `tag_id`, `state`
  `added|removed`). `index.db` `tag_suggestions` (`content_hash`, `tag_id`): the AI's
  choices per content, so copies share them and re-tagging is a stage rerun.
  **Effective tags** = your `added` ∪ ((AI suggestions ∪ the folder type's
  `default_tags`) − your `removed`). Names are `/` paths (at most 6 levels, 64
  characters a part, no `: " \ ,`); a parent name matches the tags under it. Renames
  and merges keep the old name as an alias, so links and filters keep working.
  Folder types' `default_tags` are created as tags when the type is defined.
- **Changes are actions** (journaled, undoable): `tag.add` (auto; creates a new tag),
  `tag.remove` (proposal), `tag.create`, `tag.update` (rename / describe / activate /
  deprecate), `tag.merge`. The plan's single `tag.vocab_change` is split so each has
  plain params and its own undo. Undo data is read in `apply` (state writes run after
  `inverse` is computed).
- **SDK**: `TagInfo`, `FileTag`, `Tags` (`web.tags.vocabulary()`, `retag()`),
  `FileDetails.tags`, `StageContext.tag_vocabulary()` / `write_tags(names)` for the
  stage producing artifact `tags`.
- **Re-tagging**: when a tag becomes usable, the tag stage runs again only where it was
  skipped or chose nothing (`stages.rerun(..., also=)`); tags it chose stay until you
  ask for "Re-tag every file" (`steward tags retag --all`), which costs one model call
  per file.
- **`tags` module** (built-in, on; requires `documents`): stage `tags.apply` (`mime` →
  `tags`; optional `text:ocr`, `text`, `text:caption`, `fields`; role `apply_tags`)
  offers at most `max_vocabulary` (500) tags with descriptions as a JSON-schema enum
  and keeps at most `max_tags` (5). Skips with "no tags yet" while the vocabulary is
  empty. Role `generate_tags` for the bootstrap. Page `/ui/tags`.
- **Bootstrap** (core): `steward tags bootstrap` samples contents from the normal scope
  (never locked, hidden or excluded files, and only what the role's endpoint may
  receive), asks for candidates per 20 excerpts and once more to merge them, and writes
  YAML for you to edit and `steward tags import` (additive).
- **Privacy**: tag filters and counts use the same scope conditions as search, so a
  locked file's tags show only in the Locked Folder. Backups export `tags` and
  `file_tags`.

## Consequences
- One place answers "what are this file's tags", and later features (sidecars, Immich,
  chat tools) use it without new tables.
- Adding a tag doesn't re-tag files that already have AI tags; that's a deliberate
  cost trade-off, surfaced as a button.
- The AI can't invent tags: an enum schema needs structured output (`json_schema`
  roles enforce it; others are validated and asked again).
- `tags`/`file_tags` follow the core's migrations, so a module can't change them.
