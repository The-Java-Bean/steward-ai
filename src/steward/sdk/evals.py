# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Eval suites: how well the model behind a role does a module's job (plan §11.5).

A module that owns a prompt registers a suite with `Registry.eval_suite`: a YAML file of
labeled cases it ships, and a function that runs them through the module's real prompt,
schema and cleaning, so the score says what indexing would do. `steward eval <role>`
runs it against the current model and compares with the stored baseline. Change a model
or a prompt only after comparing scores.

Cases are made up for the project: no personal data, no real people or companies. They
go to the role's model without any provenance, because they aren't anyone's files.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from steward.sdk.models import StageModel


@dataclass(frozen=True, slots=True)
class EvalCaseResult:
    case: str
    """The case's id in the set."""
    metrics: Mapping[str, float]
    """Scores from 0 (worst) to 1 (best); only those that apply to this case. A suite's
    score for a metric is its average over the cases that have it."""
    passed: bool
    note: str = ""
    """What went wrong, for `--verbose`. Not stored."""


class EvalModel(StageModel, Protocol):
    """The model behind a role, as an eval sees it."""

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        """Vectors for search queries (the role's `query_prefix` is added in front)."""
        ...

    @property
    def min_similarity(self) -> float:
        """The similarity below which search drops a match (embedding roles)."""
        ...


type EvalRun = Callable[[Mapping[str, Any], EvalModel], Iterable[EvalCaseResult]]
"""Gets the parsed cases file and the model; yields one result per case as it goes, so
progress can be shown. `ModelUnavailable` ends the run with nothing recorded."""


@dataclass(frozen=True, slots=True)
class EvalSuite:
    role: str
    cases: Path
    run: EvalRun = field(repr=False)
    primary: str
    """The metric to look at first."""
    prompt: str = ""
    """The prompt it tests (`fields.v1`), kept with the results."""
    module: str = ""
