# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`documents`: text from documents, chunked for search (plan §10.2, docs/adr/0021).

Stages:
- `documents.extract` (`mime` → `text`): plain text and code, Markdown, HTML, PDF
  (pypdf), DOCX and ODT. The parser runs in an isolated, resource-limited child process
  (`StageContext.run_isolated`). Encrypted, binary-in-disguise, unparseable, too slow or
  too big files are recorded as skipped with the reason.
- `documents.ocr` (`mime` → `text:ocr`, reading `text` when it exists): Tesseract for
  PDF pages without enough text that hold an image (every page where a folder type adds
  `ocr`) and for images; the result is the whole text with OCR'd pages filled in.
  Registered only when the `tesseract` program is found; the health check
  `documents.ocr` says when it isn't (docs/adr/0022-ocr-and-throughput.md).
- `documents.chunk` (`mime` → `chunks`, reading `text:ocr` if it holds text, else
  `text`, plus an image's `text:caption` when a module such as `photos` wrote one):
  heading- and page-aware chunks, full-text searchable through the core as soon as
  they're written.

Built only on `steward.sdk`; folder types decide where it runs (`index: full`; the
`media` type removes `extract` and `ocr`).
"""

from __future__ import annotations

import asyncio
import shutil
from functools import partial
from typing import ClassVar, cast

from pydantic import Field, field_validator

from steward.modules.documents import chunker, extractors, ocr
from steward.sdk import (
    BlobInfo,
    FileInfo,
    FolderInfo,
    HealthResult,
    IsolationError,
    ModuleConfig,
    ModuleKind,
    Registry,
    StageContext,
    StageResult,
    TextPart,
)

MIB = 1024 * 1024
EXTRACT_VERSION = 1
OCR_VERSION = 1
CHUNK_VERSION = 1
OCR_STAGE = "documents.ocr"
CAPTION = "text:caption"
"""A description of an image, written by another module (`photos.caption`): chunked
after the document's own text, so it's found by words and meaning."""

SKIP_REASONS = {
    "NotText": "not text",
    "EncryptedDocument": "encrypted",
    "UnsafeDocument": "refused: unsafe document structure",
    "OcrFailed": "OCR failed",
}
ISOLATION_REASONS = {
    "timed_out": "timed out",
    "too_big": "needed too much memory",
    "crashed": "the parser crashed",
}


class DocumentsConfig(ModuleConfig):
    max_file_mib: int = Field(200, ge=1, le=100_000)
    """Larger files aren't read (they keep name and metadata search)."""
    max_text_mib: int = Field(16, ge=1, le=1024)
    """Text kept per document, in MiB of characters; the rest is cut off."""
    max_pages: int = Field(2000, ge=1, le=1_000_000)
    timeout_s: int = Field(120, ge=5, le=3600)
    """Wall-clock time one document may take to parse."""
    memory_mib: int = Field(1024, ge=128, le=65536)
    """Address-space limit of the parser process."""
    chunk_chars: int = Field(1200, ge=200, le=20_000)
    chunk_overlap: int = Field(150, ge=0, le=5000)
    ocr: bool = True
    """OCR scans and images (needs the `tesseract` program)."""
    ocr_command: str = "tesseract"
    """The Tesseract program: a name looked up on PATH, or an absolute path."""
    ocr_languages: tuple[str, ...] = ("eng",)
    """Tesseract language names (`eng`, `deu`, `chi_sim`…); each needs its data installed."""
    ocr_images: bool = True
    """Also OCR images (PNG, JPEG, TIFF, BMP, GIF, WebP), not only PDFs."""
    ocr_dpi: int = Field(300, ge=72, le=600)
    ocr_min_chars: int = Field(20, ge=0, le=100_000)
    """PDF pages with less extracted text than this (non-space characters) are OCR'd."""
    ocr_max_pages: int = Field(500, ge=1, le=1_000_000)
    ocr_timeout_s: int = Field(900, ge=10, le=86_400)
    """Wall-clock time OCR of one document may take."""
    ocr_memory_mib: int = Field(1536, ge=256, le=65536)

    @field_validator("ocr_languages")
    @classmethod
    def _languages(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("name at least one OCR language")
        bad = [v for v in value if not ocr.valid_language(v)]
        if bad:
            raise ValueError(f"not a Tesseract language name: {', '.join(bad)}")
        return tuple(dict.fromkeys(value))


def _extract_version() -> int:
    return EXTRACT_VERSION * 100 + extractors.VERSION


def _ocr_version() -> int:
    return OCR_VERSION * 100 + ocr.VERSION


def _isolation_reason(exc: IsolationError) -> str:
    if exc.reason == "error":
        return SKIP_REASONS.get(exc.error_type or "", f"unreadable ({exc.error_type})")
    return ISOLATION_REASONS[exc.reason]


def merge_pages(text: list[TextPart], result: ocr.PdfOcr, max_chars: int) -> list[TextPart]:
    """The document's text with OCR'd pages in place of what extraction found there."""
    by_page: dict[int | None, list[TextPart]] = {}
    for part in text:
        by_page.setdefault(part.page, []).append(part)
    merged: list[TextPart] = []
    for number in range(1, result.pages + 1):
        found = result.text.get(number)
        if found:
            merged.append(TextPart(found, page=number))
        else:
            merged.extend(by_page.get(number, ()))
    merged.extend(
        p for page, parts in by_page.items() if page is None or page > result.pages for p in parts
    )
    out: list[TextPart] = []
    left = max_chars
    for part in merged:
        if left <= 0:
            break
        body = part.body[:left]
        left -= len(body)
        out.append(TextPart(body, page=part.page, heading=part.heading))
    return out


def _run_ocr(
    ctx: StageContext,
    config: DocumentsConfig,
    command: str,
    languages: tuple[str, ...],
    limits: extractors.Limits,
) -> StageResult:
    if not languages:
        ctx.write_text([], artifact="text:ocr")
        wanted = "+".join(config.ocr_languages)
        return StageResult(status="skipped", reason=f"no OCR language data ({wanted})")
    settings = ocr.Settings(
        command=command,
        languages=languages,
        dpi=config.ocr_dpi,
        min_chars=config.ocr_min_chars,
        max_pages=config.ocr_max_pages,
        budget_s=config.ocr_timeout_s * 0.8,
        force=ctx.folder.adds(OCR_STAGE),
    )
    run = partial(
        ctx.run_isolated, timeout_s=config.ocr_timeout_s, memory_mib=config.ocr_memory_mib
    )
    reason = None
    try:
        if ctx.blob.mime == ocr.PDF_MIME:
            text = ctx.read_text("text")
            page_chars: dict[int, int] = {}
            for part in text:
                if part.page is not None:
                    page_chars[part.page] = page_chars.get(part.page, 0) + ocr.nonspace(part.body)
            result = run(partial(ocr.ocr_pdf, settings=settings, page_chars=page_chars))
            if not result.text and not result.unfinished:
                ctx.write_text([], artifact="text:ocr")
                return StageResult(status="skipped", reason="has a text layer")
            parts = merge_pages(text, result, limits.max_text_chars)
            if result.unfinished:
                reason = f"OCR stopped early: {result.unfinished} pages left (time or page limit)"
        else:
            found = run(partial(ocr.ocr_image, settings=settings))
            parts = [TextPart(found[: limits.max_text_chars])] if found else []
            if not found:
                reason = "no text found"
    except IsolationError as exc:
        ctx.write_text([], artifact="text:ocr")
        return StageResult(status="skipped", reason=_isolation_reason(exc))
    ctx.write_text(parts, artifact="text:ocr")
    return StageResult(reason=reason)


class DocumentsModule:
    name: ClassVar[str] = "documents"
    version: ClassVar[str] = "0.1.0"
    kind: ClassVar[ModuleKind] = "feature"
    requires: ClassVar[tuple[str, ...]] = ()
    config_model: ClassVar[type[ModuleConfig] | None] = DocumentsConfig

    def register(self, r: Registry) -> None:
        config = r.config if isinstance(r.config, DocumentsConfig) else DocumentsConfig()
        if config.chunk_overlap >= config.chunk_chars // 2:
            raise ValueError("documents: chunk_overlap must be below half of chunk_chars")
        limits = extractors.Limits(
            max_text_chars=config.max_text_mib * MIB, max_pages=config.max_pages
        )
        max_bytes = config.max_file_mib * MIB

        def applies(blob: BlobInfo, file: FileInfo, folder: FolderInfo) -> bool:
            return blob.size <= max_bytes and (
                extractors.extractor_for(blob.mime, blob.kind) is not None
            )

        def extract(ctx: StageContext) -> StageResult:
            fn = partial(extractors.extract, mime=ctx.blob.mime, kind=ctx.blob.kind, limits=limits)
            try:
                result = ctx.run_isolated(
                    fn, timeout_s=config.timeout_s, memory_mib=config.memory_mib
                )
            except IsolationError as exc:
                ctx.write_text([])
                return StageResult(status="skipped", reason=_isolation_reason(exc))
            ctx.write_text(result.parts)
            return StageResult(reason="text cut off at the limit" if result.truncated else None)

        command = shutil.which(config.ocr_command) if config.ocr else None

        def ocr_applies(blob: BlobInfo, file: FileInfo, folder: FolderInfo) -> bool:
            if blob.size > max_bytes:
                return False
            return blob.mime == ocr.PDF_MIME or (config.ocr_images and blob.mime in ocr.IMAGE_TYPES)

        def ocr_languages() -> tuple[str, ...]:
            installed = ocr.installed_languages(command) if command else frozenset()
            return tuple(lang for lang in config.ocr_languages if lang in installed)

        def run_ocr(ctx: StageContext) -> StageResult:
            return _run_ocr(ctx, config, cast(str, command), ocr_languages(), limits)

        async def ocr_health() -> HealthResult:
            if not config.ocr:
                return HealthResult("ok", "OCR is turned off")
            if command is None:
                return HealthResult(
                    "warn",
                    f"OCR unavailable: {config.ocr_command!r} isn't installed, so scanned "
                    "documents and images aren't searchable by their text",
                )
            installed = await asyncio.to_thread(ocr.installed_languages, command)
            missing = [lang for lang in config.ocr_languages if lang not in installed]
            if len(missing) == len(config.ocr_languages):
                return HealthResult("warn", f"OCR unavailable: no data for {', '.join(missing)}")
            if missing:
                return HealthResult("warn", f"OCR language data missing: {', '.join(missing)}")
            return HealthResult("ok", f"Tesseract, languages {', '.join(config.ocr_languages)}")

        def chunk(ctx: StageContext) -> StageResult:
            source = "text:ocr"
            parts = ctx.read_text(source)
            if not parts:
                source = "text"
                parts = ctx.read_text(source)
            chunks = chunker.chunk_parts(
                parts, size=config.chunk_chars, overlap=config.chunk_overlap
            )
            caption = ctx.read_text(CAPTION)
            if caption:
                if not chunks:
                    source = CAPTION
                chunks += chunker.chunk_parts(
                    caption, size=config.chunk_chars, overlap=config.chunk_overlap
                )
            ctx.write_chunks(chunks, source=source)
            return StageResult() if chunks else StageResult(status="skipped", reason="no text")

        r.stage(
            "documents.extract",
            extract,
            version=_extract_version(),
            requires=("mime",),
            produces=("text",),
            lane="cpu",
            applies_to=applies,
        )
        if command is not None:
            r.stage(
                OCR_STAGE,
                run_ocr,
                version=_ocr_version(),
                requires=("mime",),
                optional=("text",),
                produces=("text:ocr",),
                lane="cpu",
                applies_to=ocr_applies,
            )

        def chunk_applies(blob: BlobInfo, file: FileInfo, folder: FolderInfo) -> bool:
            # Images: their OCR text, or a description another module (`photos`) wrote.
            return applies(blob, file, folder) or blob.kind == "image"

        r.stage(
            "documents.chunk",
            chunk,
            version=CHUNK_VERSION,
            requires=("mime",),
            optional=("text", "text:ocr", CAPTION),
            produces=("chunks",),
            lane="cpu",
            applies_to=chunk_applies,
        )
        r.health_check(OCR_STAGE, ocr_health)
