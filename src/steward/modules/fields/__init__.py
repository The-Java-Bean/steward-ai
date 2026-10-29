# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`fields`: facts read from documents (plan §10.2, docs/adr/0025).

Stage `fields.extract` (`mime` → `fields` properties; optional `text`, `text:ocr`):
the model mapped to the `fields` role reads the start of a document's text and answers
with a fixed set of fields (`doc_type`, `title`, `date`, `sender`, `recipient`,
`amount`, `currency`) plus the folder type's `fields`, as structured output. Values go
into the core's properties, so the file page shows them and search filters on them
(`amount>100`, `doc_type:invoice`). Runs for documents, and for images and text files
where the folder type has `fields` or adds the stage. Without a model mapped to `fields`
it doesn't run.

Built only on `steward.sdk`.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from steward.modules.fields import extract
from steward.sdk import (
    BlobInfo,
    FileInfo,
    FolderInfo,
    ModuleConfig,
    ModuleKind,
    Registry,
    StageContext,
    StageResult,
)

STAGE = "fields.extract"
ROLE = "fields"
ARTIFACT = "fields"
FIELDS_VERSION = 1
TEXT_KINDS = ("text:ocr", "text")


class FieldsConfig(ModuleConfig):
    max_chars: int = Field(6000, ge=500, le=100_000)
    """How much of the start of a document the model reads."""
    max_value_chars: int = Field(200, ge=20, le=2000)


def _version() -> int:
    return FIELDS_VERSION * 100 + extract.PROMPT_VERSION


def document_text(ctx: StageContext, max_chars: int) -> str:
    """The start of the document's text (OCR text when there is some)."""
    for artifact in TEXT_KINDS:
        parts = ctx.read_text(artifact)
        text = "\n\n".join(p.body for p in parts if p.body.strip())
        if text.strip():
            return text[:max_chars]
    return ""


class FieldsModule:
    name: ClassVar[str] = "fields"
    version: ClassVar[str] = "0.1.0"
    kind: ClassVar[ModuleKind] = "feature"
    requires: ClassVar[tuple[str, ...]] = ("documents",)
    config_model: ClassVar[type[ModuleConfig] | None] = FieldsConfig

    def register(self, r: Registry) -> None:
        config = r.config if isinstance(r.config, FieldsConfig) else FieldsConfig()
        r.model_role(
            ROLE,
            kind="chat",
            description="Reads facts (date, sender, amount…) from documents (structured output)",
        )

        def applies(blob: BlobInfo, file: FileInfo, folder: FolderInfo) -> bool:
            if blob.kind == "document":
                return True
            return blob.kind in ("image", "text") and bool(folder.fields or folder.adds(STAGE))

        def run(ctx: StageContext) -> StageResult:
            text = document_text(ctx, config.max_chars)
            if not text:
                ctx.write_properties({}, artifact=ARTIFACT)
                return StageResult("skipped", "no text")
            names = extract.field_names(ctx.folder.fields)
            answer = ctx.model(ROLE).structured(
                extract.schema(names), extract.messages(ctx.file.name, text)
            )
            ctx.write_properties(extract.clean(answer, config.max_value_chars), artifact=ARTIFACT)
            return StageResult(prompt_version=extract.PROMPT_NAME)

        r.stage(
            STAGE,
            run,
            version=_version(),
            requires=("mime",),
            optional=TEXT_KINDS,
            produces=(ARTIFACT,),
            lane="llm-local",
            applies_to=applies,
            roles=(ROLE,),
        )
