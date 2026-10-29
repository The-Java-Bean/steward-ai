# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`documents`: text from documents, chunked for search (plan §10.2, docs/adr/0021).

Stages:
- `documents.extract` (`mime` → `text`): plain text and code, Markdown, HTML, PDF
  (pypdf), DOCX and ODT. The parser runs in an isolated, resource-limited child process
  (`StageContext.run_isolated`). Encrypted, binary-in-disguise, unparseable, too slow or
  too big files are recorded as skipped with the reason.
- `documents.chunk` (`text` → `chunks`): heading- and page-aware chunks, full-text
  searchable through the core as soon as they're written.

Built only on `steward.sdk`; folder types decide where it runs (`index: full`; the
`media` type removes both stages).
"""

from __future__ import annotations

from functools import partial
from typing import ClassVar

from pydantic import Field

from steward.modules.documents import chunker, extractors
from steward.sdk import (
    BlobInfo,
    FileInfo,
    FolderInfo,
    IsolationError,
    ModuleConfig,
    ModuleKind,
    Registry,
    StageContext,
    StageResult,
)

MIB = 1024 * 1024
EXTRACT_VERSION = 1
CHUNK_VERSION = 1

SKIP_REASONS = {
    "NotText": "not text",
    "EncryptedDocument": "encrypted",
    "UnsafeDocument": "refused: unsafe document structure",
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


def _extract_version() -> int:
    return EXTRACT_VERSION * 100 + extractors.VERSION


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
                if exc.reason == "error":
                    reason = SKIP_REASONS.get(
                        exc.error_type or "", f"unreadable ({exc.error_type})"
                    )
                else:
                    reason = ISOLATION_REASONS[exc.reason]
                ctx.write_text([])
                return StageResult(status="skipped", reason=reason)
            ctx.write_text(result.parts)
            return StageResult(reason="text cut off at the limit" if result.truncated else None)

        def chunk(ctx: StageContext) -> StageResult:
            parts = ctx.read_text("text")
            chunks = chunker.chunk_parts(
                parts, size=config.chunk_chars, overlap=config.chunk_overlap
            )
            ctx.write_chunks(chunks, source="text")
            return StageResult()

        r.stage(
            "documents.extract",
            extract,
            version=_extract_version(),
            requires=("mime",),
            produces=("text",),
            lane="cpu",
            applies_to=applies,
        )
        r.stage(
            "documents.chunk",
            chunk,
            version=CHUNK_VERSION,
            requires=("text",),
            produces=("chunks",),
            lane="cpu",
            applies_to=applies,
        )
