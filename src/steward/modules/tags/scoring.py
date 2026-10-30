# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Scoring the `apply_tags` role (`steward eval apply_tags`): the real prompt and enum
schema on made-up files and one vocabulary. `f1`: the right tags, no others (`allow`
lists tags that are fine but not needed). `tagged`: files that should get a tag and did,
the >90% criterion of plan §19. `no_force`: files nothing fits stay untagged."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from steward.modules.tags import apply
from steward.sdk import EvalCaseResult, EvalModel, StructuredOutputError, TagInfo

MAX_TAGS = 5


def score(
    expected: Iterable[str], allowed: Iterable[str], chosen: Iterable[str]
) -> dict[str, float]:
    want, ok, got = set(expected), set(allowed), set(chosen)
    if not want:
        return {"no_force": 0.0 if got else 1.0}
    if not got:
        return {"f1": 0.0, "tagged": 0.0}
    hits = len(want & got)
    precision = len(got & (want | ok)) / len(got)
    recall = hits / len(want)
    f1 = 2 * precision * recall / (precision + recall) if hits else 0.0
    return {"f1": round(f1, 4), "tagged": 1.0}


def run(data: Mapping[str, Any], model: EvalModel) -> Iterator[EvalCaseResult]:
    vocabulary = [
        TagInfo(t["name"], "active", t.get("description", "")) for t in data["vocabulary"]
    ]
    names = tuple(t.name for t in vocabulary)
    for case in data["cases"]:
        expected = case.get("expect", [])
        try:
            answer = model.structured(
                apply.schema(names, MAX_TAGS),
                apply.messages(
                    vocabulary,
                    file_name=case["file"],
                    folder_type=case.get("folder_type", "documents"),
                    text=case.get("text", ""),
                    description=case.get("description", ""),
                    fields=case.get("fields", {}),
                ),
            )
        except StructuredOutputError:
            metrics = score(expected, (), ())
            yield EvalCaseResult(case["id"], metrics, False, "no valid answer")
            continue
        chosen = list(getattr(answer, "tags", []))
        metrics = score(expected, case.get("allow", []), chosen)
        yield EvalCaseResult(
            case["id"], metrics, all(v == 1.0 for v in metrics.values()), f"chose {chosen}"
        )
