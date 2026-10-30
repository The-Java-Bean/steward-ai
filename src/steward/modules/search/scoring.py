# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Scoring the `embed` role (`steward eval embed`): made-up passages found by queries
that say the same thing in other words. `recall_at_3`: a right passage is among the three
closest. `mrr`: how close to the top it is. `above_floor`: it clears the role's
`min_similarity`, or search would drop it. `rejects_unrelated`: nothing clears it for a
query the passages have no answer to."""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from steward.sdk import EvalCaseResult, EvalModel


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return dot / norm if norm else 0.0


def run(data: Mapping[str, Any], model: EvalModel) -> Iterator[EvalCaseResult]:
    documents: Mapping[str, str] = data["documents"]
    ids = list(documents)
    vectors = dict(zip(ids, model.embed([documents[i] for i in ids]), strict=True))
    wanted, unrelated = data["queries"], data["unrelated"]
    queries = [c["query"] for c in (*wanted, *unrelated)]
    query_vectors = model.embed_queries(queries)
    floor = model.min_similarity
    for case, vector in zip(wanted, query_vectors, strict=False):
        ranked = sorted(ids, key=lambda i: cosine(vector, vectors[i]), reverse=True)
        rank = next(n for n, i in enumerate(ranked, 1) if i in case["expect"])
        best = max(cosine(vector, vectors[i]) for i in case["expect"])
        metrics = {
            "recall_at_3": 1.0 if rank <= 3 else 0.0,
            "mrr": 1 / rank,
            "above_floor": 1.0 if best >= floor else 0.0,
        }
        yield EvalCaseResult(
            case["id"],
            metrics,
            rank <= 3 and best >= floor,
            f"rank {rank}, similarity {best:.2f}",
        )
    for case, vector in zip(unrelated, query_vectors[len(wanted) :], strict=True):
        best = max(cosine(vector, v) for v in vectors.values())
        yield EvalCaseResult(
            case["id"],
            {"rejects_unrelated": 1.0 if best < floor else 0.0},
            best < floor,
            f"best similarity {best:.2f}",
        )
