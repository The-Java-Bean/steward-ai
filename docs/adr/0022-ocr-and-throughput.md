# 0022. OCR with Tesseract, optional stage inputs, and measured throughput

- Status: Accepted
- Date: 2026-10-28

## Context
Scanned PDFs and pictures of documents have no text layer, so after ADR 0021 they're
findable only by name. The plan (§10.2) puts OCR between extraction and chunking:
`documents.ocr` (`text` → `text:ocr`), with `chunk` using the OCR text when there is
some. That needs a way for a stage to read an input that only exists for some contents,
which the stage framework (ADR 0011) didn't have: a required input that's `skipped` blocks
everything after it. OCR also starts an external program, which the isolation of ADR
0021 must cover, and it's the most expensive thing the pipeline has done so far, so the
plan asked for measured throughput and memory (§10.4).

## Decision
- **Optional stage inputs.** `Registry.stage(optional=(...))` names artifact kinds a
  stage reads when they exist. They need no producer, but their edges count for the
  order, cycles, `downstream` and the graph signature (graphs without optional inputs
  keep their old signature). Per content the planner works out the **pending** stages:
  wanted (enabled for a file's folder, `applies_to` yes), not current, and each required
  input usable or pending itself. A stage is queued when its required inputs are usable
  and none of its optional inputs is pending; its result is current only if it's newer
  than every current optional input. A stage reading an optional input that isn't
  usable gets an empty result, so rows left by a stage a folder type removed later
  aren't read.
- **`FolderInfo.stages_added` / `FolderInfo.adds(name)`**: whether the folder's type
  adds a stage explicitly. `documents.ocr` OCRs every page there (a `scans` type with
  `stages: {add: [ocr]}`), and only pages without text elsewhere.
- **`run_isolated` kills the process group.** The child calls `setpgid(0, 0)` first; the
  parent kills the group at the timeout and after every run, so a hung `tesseract`
  can't outlive its document. Child processes inherit the address-space and CPU limits.
- **Tesseract through its CLI, pages rendered with pypdfium2.** `documents.ocr`
  (`mime` → `text:ocr`, optional `text`, lane `cpu`):
  - PDFs: a page is OCR'd when its extracted text has fewer than `ocr_min_chars` (20)
    non-space characters **and** it contains an image (blank pages aren't), or always
    where a type adds `ocr`. pypdfium2 renders it in grayscale at `ocr_dpi` (300),
    scaled down below 40 million pixels; the bitmap goes to `tesseract stdin stdout -l
    <languages> --dpi <n>` as a PGM over stdin, one thread (`OMP_THREAD_LIMIT=1`), a
    minimal environment, `LC_ALL=C`. The result is the whole document with OCR'd pages
    in place of their extracted text. No page to OCR → `skipped` ("has a text layer"),
    and `chunk` uses `text`. After `ocr_max_pages` (500) pages or 80% of
    `ocr_timeout_s` (900 s) no new page is started; the result says how many were left.
    Password-protected PDFs are `skipped` ("encrypted").
  - Images (PNG, JPEG, TIFF, BMP, GIF, WebP; `ocr_images`, on) go to `tesseract`
    directly over stdin (first frame only).
  - Nothing is written to disk: no temporary files, no searchable PDF (steward-ai never
    changes originals).
  - Tesseract's stdout is the result; its stderr and exit details are dropped (a
    non-zero exit is `OcrFailed`, recorded as "OCR failed").
  - `ocr_languages` ([`eng`]) are intersected with `tesseract --list-langs` (probed once
    per process).
- **Availability decides registration.** `documents.ocr` is registered only when `ocr`
  is on and `ocr_command` (`tesseract`) is found. Without it nothing fails: scans keep
  name search, and the health check `documents.ocr` warns "OCR unavailable" on
  `/ui/health`. Installing it later changes the graph signature, so the worker's full
  pass back-fills every PDF and image. Missing language data is a health warning too;
  with none of the configured languages installed the stage records `skipped`.
- **`documents.chunk`** now requires only `mime` and reads `text` and `text:ocr` as
  optional inputs, preferring `text:ocr` when it has text. It also applies to images
  OCR can read, and records `skipped` ("no text") when there's nothing to chunk.
- **The image** installs Debian's `tesseract-ocr` (5.5, English and OSD data) and
  copies the copyright file of every Debian package that adds to
  `/licenses/debian/` (list in `PACKAGES`). The license collector also copies license
  files kept in a dist-info `licenses/` folder (pypdfium2 ships PDFium's build
  dependencies' licenses there). New Python dependency: `pypdfium2` (Apache-2.0 or
  BSD-3-Clause; PDFium BSD-3-Clause).
- **Not ocrmypdf + Ghostscript**, which the plan first named: ocrmypdf's job is
  writing a searchable PDF, which steward-ai never does; it would bring Ghostscript
  (AGPL) and a second Python stack into the image, and OCR whole documents where
  per-page control is what's needed. docling stays out too (a later optional module).

## Measurements
Corpus generated for the purpose (no real documents): 20 text PDFs × 10 pages, 10
scanned PDFs × 5 pages (rendered at 200 dpi, rotated up to 1°, blurred, salt-and-pepper
noise, JPEG), 5 mixed PDFs (3 typed + 2 scanned pages), 10 PNG screenshots, 5 photos of
pages (3000×4000 JPEG, rotated up to 3°), 5 photos without text (4000×3000 JPEG), 20
DOCX, 20 ODT, 20 Markdown. Run in the `steward` image on an Intel Core i7-8700 (6 cores,
12 threads), one file at a time, default limits:

| Files | Extract s/file | OCR s/file | OCR s/page | Child peak MiB | Tesseract peak MiB | Word recall |
|---|---|---|---|---|---|---|
| Text PDFs (10 pp) | 0.054 | 0.042 (nothing to OCR) | – | 68 | – | – |
| Scanned PDFs (5 pp) | 0.013 | 12.6 | 2.53 | 122 | 122 | 97.2% |
| Mixed PDFs (2 of 5 pp OCR'd) | 0.205 | 5.08 | 2.54 | 102 | 102 | 97.0% |
| Screenshots | – | 0.99 | 0.99 | 61 | 61 | 99.5% |
| Photos of pages | – | 2.68 | 2.68 | 62 | 167 | 88.9% |
| Photos without text | – | 1.12 | 1.12 | 64 | 200 | – |
| DOCX / ODT / Markdown | 0.007–0.010 | – | – | 61 | – | – |

End to end (`steward worker`, `cpu` lane concurrency 2, all 115 files from an empty
index): 99 s from the scan to the last chunk; the worker container's memory peak was
492 MiB (188 MiB afterwards).

## Consequences
- A scanned page costs about 2.5 s of one core, a photographed one about 2.7 s. A
  library with 10 000 scanned pages takes about 3.5 hours per `cpu` lane slot on first
  index; text documents are 50–1000 times cheaper.
- Photos in folders that aren't `media` are OCR'd too (about 1 s each, nothing found):
  map photo folders to `media` (which removes the documents stages) or set
  `ocr_images: false`.
- The `cpu` lane is shared: during a first index full of scans, extraction of new text
  documents waits behind OCR jobs (FIFO). Raise the lane's concurrency if there are
  cores to spare; each OCR job uses one core and up to ~350 MiB (child + Tesseract).
- The default `ocr_memory_mib` (1536) leaves ample room over the measured 200 MiB;
  `ocr_timeout_s` 900 covers about 300 scanned pages per document within the budget.
- The image grows by about 160 MB (Tesseract and its libraries; ICU is the largest).
  More languages mean `tesseract-ocr-<lang>` in a derived image or mounted data with
  `TESSDATA_PREFIX`.
- Changing `ocr_languages` doesn't re-OCR existing files (the stage version doesn't
  include it); `steward reindex` (Phase 3 chunk 6) or a version bump does.
- Tesseract doesn't detect page orientation in this mode: upside-down scans OCR badly.
- Every future stage can use optional inputs (captions, fields); the planner computes
  a little more per content (wanted checks for pending stages).
