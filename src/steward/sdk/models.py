# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Model access for modules: roles, not model names (plan §11, docs/adr/0023).

A module declares the roles it uses (`Registry.model_role`) and the stages that call
them (`Registry.stage(..., roles=...)`). The owner maps roles to models in `llm.yaml`.
A stage whose role isn't mapped doesn't run; mapping it later back-fills. Inside a stage,
`StageContext.model(role)` gives a `StageModel`; every call goes through the core's
privacy gateway with the files that hold the content as provenance.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel

RoleKind = Literal["chat", "embed"]
ROLE_PATTERN = r"^[a-z][a-z0-9_]*$"


class ModelUnavailable(Exception):
    """The model server couldn't be reached or answered with an error. Stage jobs that
    hit it wait and run again later without using up an attempt; nothing ever falls
    back to another endpoint silently."""

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s
        """When it's worth trying again, if known."""


class StructuredOutputError(Exception):
    """The model's answer still didn't match the schema after the retries. The message
    holds the validation error, never the answer."""


@dataclass(frozen=True, slots=True)
class ModelRole:
    """A role a module declares (built by `Registry.model_role`)."""

    name: str
    kind: RoleKind
    description: str
    vision: bool = False
    """The role must accept images (`capabilities: [vision]` in `llm.yaml`)."""


type Message = Mapping[str, Any]
"""An OpenAI-style chat message: `{"role": "system"|"user"|"assistant", "content": ...}`."""


class StageModel(Protocol):
    """The model behind one role, as a stage sees it."""

    @property
    def role(self) -> str: ...

    @property
    def model(self) -> str:
        """The configured model name (for logs and results; never branch on it)."""
        ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per text, in order (embedding roles). The role's
        `document_prefix` is added in front of each text."""
        ...

    def chat(self, messages: Sequence[Message], *, options: Mapping[str, Any] | None = None) -> str:
        """The answer's text (chat roles). `options` are extra request fields
        (`temperature`, `max_tokens`…)."""
        ...

    def structured[M: BaseModel](
        self, schema: type[M], messages: Sequence[Message], *, retries: int = 2
    ) -> M:
        """An answer validated against `schema`, using the strongest method the role
        supports (JSON schema, JSON mode, or instructions in the prompt). A validation
        error is sent back to the model up to `retries` times; then
        `StructuredOutputError`."""
        ...
