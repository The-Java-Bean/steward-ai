# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`steward eval`: run a role's eval suite, keep the result, compare it (docs/adr/0027).

Results stay in `state.db` so they outlive a rebuilt index: which model and prompt, on
which version of the set, with what scores and which cases failed. No model output is
kept. A baseline is the run you chose to compare with; a different model or prompt is
compared with it too, because that's the point, but a changed set isn't comparable.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import yaml
from sqlalchemy import Connection, desc, select, update

from steward.core.db import utcnow
from steward.core.db.schema import eval_runs
from steward.core.stages.context import RoleModel
from steward.sdk import EvalCaseResult, EvalSuite

if TYPE_CHECKING:
    from steward.core.runtime import Runtime

log = logging.getLogger(__name__)

REGRESSION = 0.05
"""A metric this far below the baseline is a regression (below that it's noise)."""


class EvalError(Exception):
    """The suite can't be run."""


@dataclass(frozen=True)
class EvalModelHandle(RoleModel):
    """`EvalModel`: the role's model, with no provenance: the text isn't anyone's files."""

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        from steward.core.llm.transport import EmbedRequest

        prefix = self.gateway.role_config(self.role).query_prefix
        request = EmbedRequest([prefix + t for t in texts])
        response = self.gateway.call(self.role, request, provenance=[], purpose="index").response
        return response.vectors

    @property
    def min_similarity(self) -> float:
        return self.gateway.role_config(self.role).min_similarity


@dataclass(frozen=True, slots=True)
class EvalRecord:
    run_id: int
    role: str
    model: str
    prompt: str
    set_hash: str
    cases: int
    metrics: dict[str, float]
    failed: list[str]
    seconds: float
    baseline: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Change:
    metric: str
    score: float
    baseline: float | None

    @property
    def delta(self) -> float | None:
        return None if self.baseline is None else self.score - self.baseline

    @property
    def regressed(self) -> bool:
        return self.delta is not None and round(self.delta, 4) < -REGRESSION


def load_cases(suite: EvalSuite) -> tuple[Mapping[str, Any], str]:
    """The parsed set and a hash of it (`sha256:` prefix left out)."""
    try:
        raw = suite.cases.read_bytes()
        data = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError) as exc:
        raise EvalError(f"can't read the {suite.role} eval set: {exc}") from exc
    if not isinstance(data, Mapping):
        raise EvalError(f"the {suite.role} eval set isn't a mapping")
    return data, hashlib.sha256(raw).hexdigest()


def average(results: Sequence[EvalCaseResult]) -> dict[str, float]:
    """Each metric over the cases that have it."""
    totals: dict[str, list[float]] = {}
    for result in results:
        for name, value in result.metrics.items():
            totals.setdefault(name, []).append(value)
    return {name: round(sum(v) / len(v), 4) for name, v in sorted(totals.items())}


def _record(row: Any) -> EvalRecord:
    return EvalRecord(
        row.run_id,
        row.role,
        row.model,
        row.prompt,
        row.set_hash,
        row.cases,
        dict(row.metrics),
        list(row.failed),
        row.seconds,
        row.baseline,
        row.created_at,
    )


def history(conn: Connection, role: str, *, limit: int = 20) -> list[EvalRecord]:
    """Newest first."""
    rows = conn.execute(
        select(eval_runs)
        .where(eval_runs.c.role == role)
        .order_by(desc(eval_runs.c.run_id))
        .limit(limit)
    )
    return [_record(r) for r in rows]


def baseline(conn: Connection, role: str) -> EvalRecord | None:
    row = conn.execute(
        select(eval_runs).where(eval_runs.c.role == role, eval_runs.c.baseline.is_(True))
    ).first()
    return _record(row) if row is not None else None


def set_baseline(conn: Connection, run_id: int) -> None:
    role = conn.execute(select(eval_runs.c.role).where(eval_runs.c.run_id == run_id)).scalar_one()
    conn.execute(update(eval_runs).where(eval_runs.c.role == role).values(baseline=False))
    conn.execute(update(eval_runs).where(eval_runs.c.run_id == run_id).values(baseline=True))


def compare(run: EvalRecord, against: EvalRecord | None) -> list[Change]:
    """One entry per metric of `run`. No baseline figures if the sets differ."""
    usable = against if against is not None and against.set_hash == run.set_hash else None
    return [
        Change(name, score, usable.metrics.get(name) if usable else None)
        for name, score in run.metrics.items()
    ]


def run_suite(
    runtime: Runtime,
    role: str,
    *,
    on_case: Callable[[EvalCaseResult], None] | None = None,
) -> EvalRecord:
    """Run `role`'s suite and store the result. Raises `EvalError`, or `ModelUnavailable`
    if the model goes away; a run that didn't finish isn't recorded."""
    suite = runtime.registrations.eval_suites().get(role)
    if suite is None:
        raise EvalError(f"no eval set for role {role!r}")
    state = runtime.stages.roles.get(role)
    if state is None or not state.usable:
        raise EvalError(
            f"role {role!r} can't be used: {state.problem if state else 'not declared'}"
        )
    data, set_hash = load_cases(suite)
    model = EvalModelHandle(state, runtime.gateway, ())
    started = time.monotonic()
    results = []
    for result in suite.run(data, model):
        results.append(result)
        if on_case is not None:
            on_case(result)
    if not results:
        raise EvalError(f"the {role} eval set has no cases")
    now = utcnow()
    values: dict[str, Any] = {
        "role": role,
        "model": state.model or "",
        "prompt": suite.prompt,
        "set_hash": set_hash,
        "cases": len(results),
        "metrics": average(results),
        "failed": [r.case for r in results if not r.passed],
        "seconds": round(time.monotonic() - started, 1),
        "baseline": False,
        "created_at": now,
    }
    with runtime.dbs.state.write() as conn:
        run_id = conn.execute(eval_runs.insert().values(**values)).inserted_primary_key
        values["run_id"] = int(run_id[0]) if run_id else 0
    log.info(
        "eval finished",
        extra={"fields": {"role": role, "cases": len(results), "failed": len(values["failed"])}},
    )
    return EvalRecord(**values)
