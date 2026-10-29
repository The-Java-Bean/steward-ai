# 0021. The content index, the `documents` module and full-text search

- Status: Accepted
- Date: 2026-10-28

## Context
Phase 3 starts reading what's *inside* files: text from documents, later OCR text,
captions and embeddings. Several modules produce that kind of output, and search must
apply the scope rules (locked folders, hidden folders, trash) and later the sensitivity
filters for remote models **in SQL**, in one place (ADR 0015, plan §12.2). Parsers are
also the code most exposed to hostile files (plan §21).

## Decision
- **The content index is core storage in `index.db`** (core index revision `0004`), not
  module tables: `texts` (content hash, artifact kind such as `text` or `text:ocr`,
  `part`, `page`, `heading`, `body`), `chunks` (content hash, artifact kind, `seq`,
  `source` = the text artifact it was made from, `page`, `heading`, `body`) and
  `chunks_fts`, an external-content FTS5 table (`unicode61 remove_diacritics 2`, its
  `rank` set to BM25 with headings weighted 2:1) kept in sync by triggers. Everything is
  keyed by content hash, so moves are free and copies share one entry; rows go when
  their blob goes (`ON DELETE CASCADE`).
- **Stages write it through `StageContext`**: `write_text(parts, artifact=)`,
  `read_text(artifact=)`, `write_chunks(chunks, source=, artifact=)`, `read_chunks()`.
  Writes must name an artifact the stage `produces`, reads one it `requires` (or
  produces); each write replaces that content's rows for that artifact in one
  transaction. The core strips NUL and the private-use snippet markers
  (`MATCH_MARKERS`) and makes the text valid UTF-8.
- **Parsing runs isolated.** `StageContext.run_isolated(fn, timeout_s=, memory_mib=)`
  runs `fn(file)` in a child process from a forkserver (never a fork of the
  multi-threaded worker): `RLIMIT_AS`, a CPU-time limit, a wall-clock kill, its own
  `O_NOFOLLOW` open with the size/mtime check. Results come back pickled; failures as
  `IsolationError(reason)` (`timed_out`, `too_big`, `crashed`, `error` + exception
  *type name*), never a message, which could quote file content. The forkserver
  preloads the SDK, the isolation module and the first function's module, so a child
  costs about 5 ms instead of the imports (about 75 ms).
- **`documents`** is a built-in feature module (on by default) with two stages:
  `documents.extract` (`mime` → `text`, lane `cpu`) and `documents.chunk` (`text` →
  `chunks`). Extractors: plain text and code (BOM, UTF-8, else cp1252; NUL bytes → not
  text), Markdown (frontmatter dropped, ATX headings outside code fences start
  sections), HTML (stdlib parser; scripts, styles and `<head>` dropped; headings start
  sections), PDF (pypdf per page; an empty user password is tried, otherwise
  "encrypted"; `cryptography` is a dependency because many such PDFs use AES), DOCX and
  ODT (zip + expat with entity declarations refused and members capped, so no entity
  or zip bombs). Unreadable, encrypted, unsafe, too slow and too big files are
  `skipped` with a reason, not failed (retrying wouldn't help). Limits come from the
  module config (`max_file_mib`, `max_text_mib`, `max_pages`, `timeout_s`,
  `memory_mib`). Chunks are about `chunk_chars` (1200) characters with `chunk_overlap`
  (150), never across pages, short sections on a page packed together, long ones split
  at paragraphs, then sentences, then words.
- **No `Extractor` extension point yet.** `documents` keeps its extractors internal
  (their version is part of the stage version) until a second module needs to add one.
- **Search** (`steward.core.search`): `search_text` matches every query word (each one
  quoted, so nothing is FTS syntax) in one chunk, ranks files by their best chunk and
  returns a snippet as `(text, is_match)` segments plus the page; `search_all` merges
  name and text rankings with reciprocal rank fusion (k = 60, at most 1000 deep), the
  merge hybrid search will reuse. Both share one condition builder with name search,
  so the scope rules are identical. `/ui/search`, `/api/v1/search` (`in=all|names|text`,
  default `all`), `steward search --in` and the Locked Folder's search use it;
  `steward files text LIB:PATH` prints what was extracted (host only).
- **MIME types are detected the same on every host**: discovery uses Python's built-in
  table plus its own list (office formats included), never `/etc/mime.types`, which a
  slim container image doesn't have (found when `.docx` files weren't extracted in the
  image).

## Consequences
- Content search finds whole words (no substring or prefix matching); accents and case
  don't matter. Name search keeps substring matching.
- A file's text is found through any visible file with the same content; the same
  content in a locked folder doesn't make the visible copy unsearchable, and the locked
  copy is only found in the Locked Folder.
- Stale text of content no present file has any more stays in `index.db` until the
  blob is pruned (later: `steward reindex` / pruning); search joins on present files,
  so it's never shown.
- docling (layout models, ~1–2 GB RAM) isn't part of this; it may come later as an
  optional extractor module. OCR comes next (`text:ocr`, chunk 2 of Phase 3).
- `make check` runs the parsers in real child processes; the isolation tests cover
  timeouts, the memory limit, crashes, symlinks and changed files.
