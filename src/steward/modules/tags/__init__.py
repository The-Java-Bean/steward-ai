# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`tags`: tags chosen by a model, and the vocabulary page (plan §13.8, docs/adr/0025).

The core keeps the vocabulary and your decisions and computes each file's tags (yours,
the folder type's `default_tags`, the AI's), so search, the file manager and other
modules see the same thing. This module adds:
- stage `tags.apply` (`mime` → `tags`; optional `text`, `text:ocr`, `text:caption`,
  `fields`): the model mapped to the `apply_tags` role picks at most `max_tags` tags
  from the vocabulary (a JSON-schema enum, so it can't invent any) for documents,
  images and text files. With fewer than `min_vocabulary` tags it skips (a model
  offered one or two tags tends to force them onto everything); it runs again where it
  skipped, or chose nothing, when tags are added.
- role `generate_tags`, used by `steward tags bootstrap` to suggest a first vocabulary.
- page `/ui/tags`: the vocabulary tree with file counts, AI-created tags to review,
  new / rename / describe / deprecate / merge (journaled, with Undo), re-tagging.

Built only on `steward.sdk`.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import Field

from steward.modules.tags import apply
from steward.modules.tags.routes import PAGE, router
from steward.sdk import (
    TAGS_ARTIFACT,
    BlobInfo,
    FileInfo,
    FolderInfo,
    ModuleConfig,
    ModuleKind,
    Registry,
    StageContext,
    StageResult,
)

HERE = Path(__file__).parent
STAGE = "tags.apply"
ROLE = "apply_tags"
GENERATE_ROLE = "generate_tags"
APPLY_VERSION = 1
TEXT_KINDS = ("text:ocr", "text")
CAPTION = "text:caption"
FIELDS = "fields"


class TagsConfig(ModuleConfig):
    max_tags: int = Field(5, ge=1, le=20)
    """The most tags the model may choose for one file."""
    max_chars: int = Field(3000, ge=200, le=50_000)
    """How much of the start of a file's text the model reads."""
    max_vocabulary: int = Field(500, ge=10, le=5000)
    """Tags offered to the model (the first ones by name when there are more)."""
    min_vocabulary: int = Field(5, ge=1, le=1000)
    """Files aren't tagged by the model until there are this many tags."""


def _version() -> int:
    return APPLY_VERSION * 100 + apply.PROMPT_VERSION


def _text(ctx: StageContext, artifact: str, max_chars: int) -> str:
    return "\n\n".join(p.body for p in ctx.read_text(artifact) if p.body.strip())[:max_chars]


class TagsModule:
    name: ClassVar[str] = "tags"
    version: ClassVar[str] = "0.1.0"
    kind: ClassVar[ModuleKind] = "feature"
    requires: ClassVar[tuple[str, ...]] = ("documents",)
    config_model: ClassVar[type[ModuleConfig] | None] = TagsConfig

    def register(self, r: Registry) -> None:
        config = r.config if isinstance(r.config, TagsConfig) else TagsConfig()
        r.model_role(ROLE, kind="chat", description="Chooses tags for files from your vocabulary")
        r.model_role(
            GENERATE_ROLE,
            kind="chat",
            description="Suggests a tag vocabulary from samples of your files (a larger model)",
        )

        def applies(blob: BlobInfo, file: FileInfo, folder: FolderInfo) -> bool:
            return blob.kind in ("document", "image", "text")

        def run(ctx: StageContext) -> StageResult:
            vocabulary = ctx.tag_vocabulary()[: config.max_vocabulary]
            if len(vocabulary) < config.min_vocabulary:
                ctx.write_tags([])
                reason = (
                    "no tags yet"
                    if not vocabulary
                    else f"fewer than {config.min_vocabulary} tags yet"
                )
                return StageResult("skipped", reason)
            text = ""
            for artifact in TEXT_KINDS:
                text = text or _text(ctx, artifact, config.max_chars)
            description = _text(ctx, CAPTION, config.max_chars)
            fields = ctx.read_properties(FIELDS)
            if not (text or description or fields):
                ctx.write_tags([])
                return StageResult("skipped", "no text or description")
            names = tuple(t.name for t in vocabulary)
            answer = ctx.model(ROLE).structured(
                apply.schema(names, config.max_tags),
                apply.messages(
                    vocabulary,
                    file_name=ctx.file.name,
                    folder_type=ctx.folder.type,
                    text=text,
                    description=description,
                    fields=fields,
                ),
            )
            chosen = list(dict.fromkeys(getattr(answer, "tags", [])))[: config.max_tags]
            ctx.write_tags(chosen)
            return StageResult(prompt_version=apply.PROMPT_NAME)

        r.stage(
            STAGE,
            run,
            version=_version(),
            requires=("mime",),
            optional=(*TEXT_KINDS, CAPTION, FIELDS),
            produces=(TAGS_ARTIFACT,),
            lane="llm-local",
            applies_to=applies,
            roles=(ROLE,),
        )
        r.ui_page(
            "tags.page",
            router,
            path=PAGE,
            title="Tags",
            templates=HERE / "templates",
            static=HERE / "static",
        )
