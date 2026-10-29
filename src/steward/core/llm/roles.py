# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Roles modules declare, matched against `llm.yaml` (plan §11.1, docs/adr/0023)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from steward.core.config import LlmConfig
from steward.core.llm.gateway import model_key
from steward.core.loader import ModuleLoadError
from steward.sdk import ModelRole, RoleKind


class RoleDeclarationError(ModuleLoadError):
    """Two modules declare the same role differently."""


@dataclass(frozen=True, slots=True)
class RoleState:
    name: str
    kind: RoleKind
    description: str
    modules: tuple[str, ...]
    """The modules that declare it."""
    vision: bool
    endpoint: str | None
    model: str | None
    problem: str | None
    """Why the role can't be used (None: it can)."""
    model_key: str | None
    """`gateway.model_key` of the mapping, when usable."""

    @property
    def usable(self) -> bool:
        return self.problem is None


def resolve_roles(
    declared: Mapping[str, Sequence[tuple[str, ModelRole]]], config: LlmConfig
) -> dict[str, RoleState]:
    """Role name -> its state. Raises `RoleDeclarationError` on conflicting kinds."""
    states: dict[str, RoleState] = {}
    for name, entries in sorted(declared.items()):
        kinds = {role.kind for _, role in entries}
        if len(kinds) > 1:
            who = ", ".join(f"{module} ({role.kind})" for module, role in entries)
            raise RoleDeclarationError(f"role {name!r} is declared with different kinds: {who}")
        first = entries[0][1]
        vision = any(role.vision for _, role in entries)
        mapping = config.roles.get(name)
        problem: str | None = None
        if mapping is None:
            problem = "not mapped to a model in llm.yaml"
        elif vision and "vision" not in mapping.capabilities:
            problem = "needs a model with `capabilities: [vision]`"
        states[name] = RoleState(
            name=name,
            kind=first.kind,
            description=first.description,
            modules=tuple(dict.fromkeys(module for module, _ in entries)),
            vision=vision,
            endpoint=mapping.endpoint if mapping else None,
            model=mapping.model if mapping else None,
            problem=problem,
            model_key=(
                model_key(mapping, embed=first.kind == "embed")
                if mapping is not None and problem is None
                else None
            ),
        )
    return states
