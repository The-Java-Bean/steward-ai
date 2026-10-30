# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`search`: searching by meaning (plan §12.2, docs/adr/0023).

Declares the `embed` model role and the stage `search.embed` (`chunks` → `embeddings`,
lane `llm-local`): every chunk's heading and text are embedded with the role's model
and stored in the core's embedding space for that model. The core's search then merges
the closest chunks with name and word matches (reciprocal rank fusion), with the same
privacy and scope rules as everything else.

Without a model mapped to `embed` in `llm.yaml` the stage doesn't run and search works
by names and words only; mapping one (or changing it) embeds everything in the
background.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import Field

from steward.modules.search import scoring
from steward.sdk import ModuleConfig, ModuleKind, Registry, StageContext, StageResult

HERE = Path(__file__).parent
EMBED_VERSION = 1
ROLE = "embed"


class SearchConfig(ModuleConfig):
    batch_size: int = Field(16, ge=1, le=256)
    """Chunks sent to the model per request."""
    max_chunks: int = Field(5000, ge=1, le=1_000_000)
    """Documents with more chunks aren't embedded (they stay searchable by words)."""


def chunk_text(heading: str | None, body: str) -> str:
    return f"{heading}\n\n{body}" if heading else body


class SearchModule:
    name: ClassVar[str] = "search"
    version: ClassVar[str] = "0.1.0"
    kind: ClassVar[ModuleKind] = "feature"
    requires: ClassVar[tuple[str, ...]] = ("documents",)
    config_model: ClassVar[type[ModuleConfig] | None] = SearchConfig

    def register(self, r: Registry) -> None:
        config = r.config if isinstance(r.config, SearchConfig) else SearchConfig()
        r.model_role(
            ROLE,
            kind="embed",
            description="Embeds text for searching by meaning (an embedding model)",
        )

        def embed(ctx: StageContext) -> StageResult:
            chunks = ctx.read_chunks()
            if len(chunks) > config.max_chunks:
                ctx.write_embeddings([], role=ROLE)
                return StageResult("skipped", f"more than {config.max_chunks} chunks")
            model = ctx.model(ROLE)
            texts = [chunk_text(c.heading, c.body) for c in chunks]
            vectors: list[list[float]] = []
            for i in range(0, len(texts), config.batch_size):
                vectors.extend(model.embed(texts[i : i + config.batch_size]))
                ctx.renew_lease()
            ctx.write_embeddings(vectors, role=ROLE)
            return StageResult()

        r.stage(
            "search.embed",
            embed,
            version=EMBED_VERSION,
            requires=("chunks",),
            produces=("embeddings",),
            lane="llm-local",
            roles=(ROLE,),
        )
        r.eval_suite(ROLE, scoring.run, cases=HERE / "evals" / "embed.yaml", primary="recall_at_3")
