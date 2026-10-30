# 0030. Text by content, RAW photos by their embedded preview

- Status: Accepted
- Date: 2026-10-30

## Context
Testing on real files found three gaps in what steward-ai recognizes:

- File types came from a table of extensions. Anything else was `application/octet-stream`,
  so TypeScript, SCSS and other code wasn't extracted or searchable.
- `.cr3` wasn't in the table, so the `ftyp` check (CR3 is an ISO media file) made it
  `video/mp4`.
- RAW photos had metadata but no description, so no AI tags. In the test library they were
  a quarter of all still images.

## Decision
**Text by content.** An unknown extension with no known magic number is `text/plain` if
its first 4 KiB are UTF-8 with no NUL or control bytes. A list of extensions would be out
of date at once, and a binary file with such an extension (an MPEG `.ts`) is left alone.
`.log` stays out, rotated ones too (`app.log.1`): logs are noisy, large and often hold
tokens, so they shouldn't be embedded by default. Other encodings stay binary.

**CR3 and other RAW types** are in the table, and the `crx ` brand is recognized for files
with no useful extension.

**RAW by the embedded preview.** `photos.caption` also takes RAW files. One exiftool run in
the isolated child reads `PreviewImage`, `JpgFromRaw`, `OtherImage` and the RAW's
orientation (embedded previews usually don't carry their own), and the first preview
that's a JPEG of at least `caption_min_side` goes through `images.prepare`. Reading them
together keeps it to one process per file. Module setting `raw_previews` (default on).

Decoding RAW instead was rejected: LibRaw is a large native dependency parsing hostile
files, and a description needs only a small view of the picture, which the camera
already made.

`images.VERSION` isn't raised: nothing changes for images already described, so they
aren't sent to the model again, and the daily full pass picks up RAW files.

Measured on real files with a real vision model: 35 of 35 Canon RAW files described and
tagged. With a vocabulary built from other files, 132 of 133 photos got a tag.

## Consequences
- File types are detected while hashing, so an existing install needs `steward reindex`
  to reclassify files.
- More files go through extraction and to the model: code in `full` folders, which the
  `projects` type wants. The content scan (ADR 0029) sees all of it first.
- Phones that store RAW with only a tiny uncompressed thumbnail have no usable preview,
  and those files are skipped with that reason.
- A first back-fill of a large RAW library costs one model call per file, about 2–3 s each.
- HEIC still needs a decoder, so it's still metadata only.
