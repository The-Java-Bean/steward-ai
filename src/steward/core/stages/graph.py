# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The stage graph: every enabled stage, ordered so producers come before consumers.

Built once at startup from the registrations. Fails closed (stops startup) if two
stages produce the same artifact kind, a stage requires a kind nobody produces, or the
inputs (required and optional) form a cycle. An optional input nobody produces is
simply never there.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property

from steward.core.discovery.hashing import HASH_STAGE
from steward.core.loader import ModuleLoadError
from steward.sdk import Stage


class StageGraphError(ModuleLoadError):
    """The registered stages don't form a valid graph."""


@dataclass(frozen=True)
class StageGraph:
    stages: Mapping[str, Stage]
    """Every stage, in dependency order (the root `core.hash` first)."""
    producers: Mapping[str, str]
    """Artifact kind -> the stage that produces it."""

    @property
    def root(self) -> str:
        return HASH_STAGE

    def upstream(self, name: str) -> tuple[str, ...]:
        """The stages whose output `name` requires directly."""
        return tuple(dict.fromkeys(self.producers[k] for k in self.stages[name].requires))

    def optional_upstream(self, name: str) -> tuple[str, ...]:
        """The enabled stages whose output `name` reads when it's available."""
        return tuple(
            dict.fromkeys(
                self.producers[k] for k in self.stages[name].optional if k in self.producers
            )
        )

    @cached_property
    def downstream(self) -> Mapping[str, tuple[str, ...]]:
        """Stage -> every stage that depends on it, directly or not, in graph order."""
        result: dict[str, set[str]] = {name: set() for name in self.stages}
        for name in reversed(list(self.stages)):
            for up in (*self.upstream(name), *self.optional_upstream(name)):
                result[up] |= {name} | result[name]
        order = list(self.stages)
        return {name: tuple(sorted(deps, key=order.index)) for name, deps in result.items()}

    def matching(self, names: frozenset[str] | set[str]) -> set[str]:
        """Stages referred to by `names` (folder-type stage lists use full or short names)."""
        return {s.name for s in self.stages.values() if s.name in names or s.short_name in names}

    @cached_property
    def signature(self) -> str:
        """Changes whenever a stage is added, removed, or changes version or shape. A
        changed signature at startup triggers a full planning pass."""
        shape = [
            [s.name, s.version, s.requires, s.produces, s.lane, s.default, s.index]
            + ([s.optional] if s.optional else [])
            for s in self.stages.values()
        ]
        return hashlib.sha256(json.dumps(shape).encode()).hexdigest()


def build_graph(stages: Mapping[str, Stage]) -> StageGraph:
    if HASH_STAGE not in stages:
        raise StageGraphError(f"the root stage {HASH_STAGE!r} is not registered")
    producers: dict[str, str] = {}
    for stage in stages.values():
        for kind in stage.produces:
            if kind in producers:
                raise StageGraphError(
                    f"artifact {kind!r} is produced by both {producers[kind]!r} and {stage.name!r}"
                )
            producers[kind] = stage.name
    for stage in stages.values():
        missing = [k for k in stage.requires if k not in producers]
        if missing:
            raise StageGraphError(
                f"stage {stage.name!r} requires {', '.join(map(repr, missing))}, "
                "which no enabled stage produces (is a module missing from `requires`?)"
            )

    # Kahn's algorithm; alphabetical among stages that are ready at the same time.
    needs = {
        s.name: {producers[k] for k in (*s.requires, *s.optional) if k in producers} - {s.name}
        for s in stages.values()
    }
    order: list[str] = []
    while needs:
        ready = sorted(name for name, deps in needs.items() if not deps)
        if not ready:
            raise StageGraphError("stage inputs form a cycle: " + ", ".join(sorted(needs)))
        for name in ready:
            order.append(name)
            del needs[name]
        for deps in needs.values():
            deps.difference_update(ready)
    if order[0] != HASH_STAGE:
        # Only possible if another stage requires nothing; the root still goes first.
        order.remove(HASH_STAGE)
        order.insert(0, HASH_STAGE)
    return StageGraph({name: stages[name] for name in order}, producers)
