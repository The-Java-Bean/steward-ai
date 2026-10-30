# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Scoring the `fields` role (`steward eval fields`): the real prompt, schema and cleaning
on made-up documents. `accuracy`: stated facts read correctly. `no_guess`: facts a
document doesn't state left empty, which matters as much: a wrong amount is worse than
none."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from steward.modules.fields import extract
from steward.sdk import EvalCaseResult, EvalModel, StructuredOutputError


def _right(name: str, expected: Any, got: Any) -> bool:
    if got is None:
        return False
    if name == "amount":
        return abs(float(expected) - float(got)) < 0.005
    if name in ("sender", "recipient"):
        want, have = str(expected).casefold(), str(got).casefold()
        return want in have or have in want
    return str(expected).casefold() == str(got).casefold()


def score(
    expected: Mapping[str, Any], got: Mapping[str, Any]
) -> tuple[dict[str, float], list[str]]:
    """(metrics, the fields it got wrong)."""
    wrong = []
    stated = {k: v for k, v in expected.items() if v is not None}
    empty = [k for k, v in expected.items() if v is None]
    for name, value in stated.items():
        if not _right(name, value, got.get(name)):
            wrong.append(name)
    wrong.extend(name for name in empty if got.get(name) is not None)
    metrics: dict[str, float] = {}
    if stated:
        metrics["accuracy"] = sum(k not in wrong for k in stated) / len(stated)
    if empty:
        metrics["no_guess"] = sum(k not in wrong for k in empty) / len(empty)
    return metrics, wrong


def run(
    data: Mapping[str, Any], model: EvalModel, *, max_value_chars: int
) -> Iterator[EvalCaseResult]:
    names = extract.field_names(())
    for case in data["cases"]:
        expected = case["expect"]
        try:
            answer = model.structured(
                extract.schema(names), extract.messages(case["file"], case["text"])
            )
        except StructuredOutputError:
            yield EvalCaseResult(
                case["id"], {"accuracy": 0.0, "no_guess": 0.0}, False, "no valid answer"
            )
            continue
        got = extract.clean(answer, max_value_chars)
        metrics, wrong = score(expected, got)
        note = ", ".join(f"{name}: {got.get(name)!r}" for name in wrong)
        yield EvalCaseResult(case["id"], metrics, not wrong, note)
