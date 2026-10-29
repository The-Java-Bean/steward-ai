# 0024. Photos: EXIF with exiftool, captions from a vision role, properties in the content index

- Status: Accepted
- Date: 2026-10-29

## Context
Phase 3 chunk 4 makes photos useful: the date, camera and place they were taken, and a
description so they can be found by words and by meaning like documents (plan §10.2,
§10.3). EXIF is the first structured metadata per content; extracted fields (chunk 5)
are the second. Both must be shown by the file manager and filtered by the core's
search, and neither of those may read a module's tables. The plan named exiftool via
pyexiftool, a `caption` stage requiring `exif`, XMP sidecars and an `ImageSearch`
extension point in the same chunk.

## Decision
- **Properties are core storage**, like text and chunks (ADR 0021): index revision
  `0006` adds `properties` (`content_hash`, `artifact`, `key`, `type`
  `text|int|float|bool`, `text`, `num`; cascades from `blobs`), so SQL can compare
  strings (ISO dates) and numbers. Stages write them with
  `StageContext.write_properties(values, artifact=)` (replaces the content's rows for
  that artifact; snake_case names, at most 200, strings up to 2000 characters; None and
  empty strings left out) and read them with `read_properties`. `Files.details(path,
  scope=)` returns `FileDetails(description, properties)` for module pages, with the
  same scope rules as `Files.entry`; the file manager's file page shows a "Details"
  box. `steward files info` prints stage results and properties. Search filters on
  properties come with chunk 5.
- **`photos` module** (built-in, on). `photos.exif` (`mime` → `exif`, lane `cpu`,
  `index="metadata"`, images and videos) runs the exiftool CLI in the isolated child
  (`run_isolated`) with a fixed tag list (`-json -n -G0`), a minimal environment and
  no stderr, and stores normalized properties (`taken`, `camera`, `lens`, `width`,
  `height`, `orientation`, `rotation`, `latitude`, `longitude`, `altitude`,
  `duration_s`). QuickTime `CreateDate` is UTC by the spec; `CreationDate` (Apple's
  local time with offset) and EXIF `DateTimeOriginal` + `OffsetTimeOriginal` win over
  it; GPS 0,0 is dropped. The stage is registered only when `exiftool` is found
  (health check `photos.exif`), like OCR (ADR 0022).
- **The file is exiftool's stdin, named `/dev/stdin`**, not `-`: exiftool reads `-`
  into memory to seek in it (a 1 GiB file failed under the 512 MiB limit), while the
  path re-opens the same already-checked file, seekable (1 GiB in well under a second,
  inside the limit). No pyexiftool: one dependency less for a single JSON call.
- **`photos.caption`** (`mime` → `text:caption`, lane `llm-local`, role `caption`:
  kind chat, `vision`; JPEG, PNG, WebP, GIF, TIFF, BMP up to 100 MiB). Pillow in the
  isolated child opens only those formats, refuses more than 100 megapixels, skips
  images under 256 px on their shorter side, decodes JPEGs in draft mode, applies the
  EXIF orientation, flattens transparency onto white and sends a JPEG of at most
  1024 px with a versioned prompt (`prompts/caption/caption.v1.md`; the version folds
  into the stage version and is stored as `prompt_version`). The answer is cleaned
  (reasoning blocks, labels, wrapping quotes, Markdown bold) and capped. It doesn't
  require `exif`: Pillow reads the orientation itself, and descriptions shouldn't
  depend on exiftool being installed.
- **Captions are searched through the existing pipeline.** `documents.chunk` takes
  `text:caption` as one more optional input and appends its chunk after the
  document's own (OCR text of a photo of a receipt and its description both count);
  images now apply to `chunk`, and the `media` type no longer removes it (it still
  removes `extract` and `ocr`). `search.embed` embeds those chunks unchanged, so
  photos are found by words and by meaning with the same scope rules.
- **`reasoning` per role in `llm.yaml`** (`default|none|low|medium|high`, sent as
  `reasoning_effort` on chat requests unless the caller set it). The reference vision
  model reasons before answering: with `max_tokens` 512 it sometimes spent all of them
  reasoning and answered nothing; with `reasoning: none` it answers in about 3 s
  instead of 10 s per photo.
- **Moved out:** XMP sidecars (`sidecar.sync`) become their own chunk after tags:
  keywords come from tags, and running an action on the executor lane from a module
  job needs SDK support that doesn't exist yet. `ImageSearch` waits for its first
  consumer (chat tools in Phase 4, the `immich` module): captions already are in the
  core's search, and `kind=image` filters to photos.
- New dependency **Pillow** (MIT-CMU; added to the license allow-list; its wheel ships
  the licenses of the libraries it bundles) and Debian's `libimage-exiftool-perl`
  (Artistic or GPL; a separate program) in the image.

## Measurements
In the container image against the reference vision model on the owner's model server
(sandbox, nine public sample images plus a 1 GiB file): all images described in 16 s
end to end including embeddings (about 2 s per photo with `reasoning: none`), worker
167 MiB. "kitty" finds the cat photo, "morning espresso" the coffee, "galaxies" the
Hubble field; a copy in the Locked Folder stays out of normal results.

## Consequences
- Any module can attach structured metadata to content, and chunk 5's fields use the
  same table, so search filters and the file page are written once.
- `documents` now chunks other modules' text by an agreed artifact name
  (`text:caption`); the well-known kinds are listed in `docs/modules.md`.
- Every image gets a cheap `chunk` job (skipped "no text" when nothing wrote text).
- Images Pillow can't decode (HEIC, camera RAW) get EXIF but no description yet.
- Changing the caption prompt or model describes every photo again; changing
  `reasoning` doesn't (it isn't part of the model key).
