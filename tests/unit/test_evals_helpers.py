# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The scoring functions, the averages, and the sets the project ships: well-formed,
self-consistent and made up."""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from PIL import Image

from steward.core import evals
from steward.modules.fields import scoring as fields_scoring
from steward.modules.photos import scoring as photo_scoring
from steward.modules.search import scoring as search_scoring
from steward.modules.tags import scoring as tag_scoring
from steward.sdk import EvalCaseResult

MODULES = Path(__file__).parents[2] / "src" / "steward" / "modules"
SETS = sorted(MODULES.glob("*/evals/*.yaml"))


def load(name: str) -> dict[str, Any]:
    return dict(yaml.safe_load(next(p for p in SETS if p.stem == name).read_text()))


def test_all_four_sets_exist() -> None:
    assert sorted(p.stem for p in SETS) == ["apply_tags", "caption", "embed", "fields"]


@pytest.mark.parametrize("path", SETS, ids=lambda p: p.stem)
def test_sets_hold_nothing_personal(path: Path) -> None:
    text = path.read_text()
    assert not re.search(r"[\w.+-]+@[\w-]+\.\w+", text)  # e-mail addresses
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text)  # IPs
    assert not re.search(r"https?://", text)
    assert not re.search(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b", text)  # card numbers
    assert not re.search(r"/(home|mnt|srv)/", text)


def test_case_ids_are_unique() -> None:
    for name in ("fields", "apply_tags", "caption"):
        ids = [c["id"] for c in load(name)["cases"]]
        assert len(ids) == len(set(ids)), name
    data = load("embed")
    ids = [c["id"] for c in (*data["queries"], *data["unrelated"])]
    assert len(ids) == len(set(ids))


def test_tag_cases_use_only_the_vocabulary() -> None:
    data = load("apply_tags")
    names = {t["name"] for t in data["vocabulary"]}
    assert len(data["vocabulary"]) >= 5
    for case in data["cases"]:
        assert {*case.get("expect", []), *case.get("allow", [])} <= names, case["id"]
        assert case.get("text") or case.get("description"), case["id"]
    assert sum(1 for c in data["cases"] if not c.get("expect")) >= 3


def test_embed_queries_point_at_passages() -> None:
    data = load("embed")
    for case in data["queries"]:
        assert set(case["expect"]) <= set(data["documents"])


def test_every_caption_case_has_a_drawing() -> None:
    for case in load("caption")["cases"]:
        assert case["draw"] in photo_scoring.DRAWINGS
    assert {c["draw"] for c in load("caption")["cases"]} == set(photo_scoring.DRAWINGS)


@pytest.mark.parametrize("name", sorted(photo_scoring.DRAWINGS))
def test_drawings_are_real_images(name: str) -> None:
    image = Image.open(io.BytesIO(photo_scoring.draw(name)))
    assert image.size == photo_scoring.SIZE
    assert len(image.convert("RGB").resize((32, 32)).getcolors(1024) or ()) > 1


def test_field_scores() -> None:
    expected = {"amount": 12.5, "sender": "Harbor Lane Hardware", "recipient": None, "date": "2025"}
    good = {"amount": 12.5, "sender": "harbor lane hardware ltd", "recipient": None, "date": "2025"}
    assert fields_scoring.score(expected, good) == ({"accuracy": 1.0, "no_guess": 1.0}, [])
    bad = {"amount": 12.0, "sender": "Elsewhere", "recipient": "Someone", "date": "2025"}
    metrics, wrong = fields_scoring.score(expected, bad)
    assert metrics["accuracy"] == pytest.approx(1 / 3)
    assert metrics["no_guess"] == 0.0
    assert sorted(wrong) == ["amount", "recipient", "sender"]


def test_tag_scores() -> None:
    assert tag_scoring.score(["a"], [], ["a"]) == {"f1": 1.0, "tagged": 1.0}
    assert tag_scoring.score(["a"], ["b"], ["a", "b"]) == {"f1": 1.0, "tagged": 1.0}
    assert tag_scoring.score(["a"], [], ["a", "c"]) == {"f1": 0.6667, "tagged": 1.0}
    assert tag_scoring.score(["a"], [], ["c"]) == {"f1": 0.0, "tagged": 1.0}
    assert tag_scoring.score(["a"], [], []) == {"f1": 0.0, "tagged": 0.0}
    assert tag_scoring.score([], [], []) == {"no_force": 1.0}
    assert tag_scoring.score([], [], ["a"]) == {"no_force": 0.0}


def test_caption_scores() -> None:
    must = [["red"], ["circle", "round"]]
    assert photo_scoring.score("A round red shape.", must, ["person"])[0] == {
        "described": 1.0,
        "concepts": 1.0,
        "no_invention": 1.0,
    }
    metrics, wrong = photo_scoring.score("A person on a red bench.", must, ["person"])
    assert metrics["concepts"] == 0.5
    assert metrics["no_invention"] == 0.0
    assert wrong == ["missing circle", "invented person"]
    assert photo_scoring.score("", must, [])[0]["described"] == 0.0
    assert photo_scoring.score("redwood", [["red"]], [])[0]["concepts"] == 0.0  # whole words


def test_cosine() -> None:
    assert search_scoring.cosine([1, 0], [1, 0]) == 1
    assert search_scoring.cosine([1, 0], [0, 1]) == 0
    assert search_scoring.cosine([0, 0], [1, 0]) == 0


def test_averages_count_only_the_cases_that_have_a_metric() -> None:
    results = [
        EvalCaseResult("a", {"f1": 1.0}, True),
        EvalCaseResult("b", {"f1": 0.5, "no_force": 0.0}, False),
    ]
    assert evals.average(results) == {"f1": 0.75, "no_force": 0.0}


def _record(metrics: dict[str, float], set_hash: str = "s") -> evals.EvalRecord:
    from steward.core.db import utcnow

    return evals.EvalRecord(1, "r", "m", "p", set_hash, 1, metrics, [], 0.0, False, utcnow())


def test_a_small_drop_is_noise_and_a_bigger_one_a_regression() -> None:
    base = _record({"f1": 0.90, "tagged": 0.95})
    changes = {c.metric: c for c in evals.compare(_record({"f1": 0.85, "tagged": 0.89}), base)}
    assert not changes["f1"].regressed  # exactly the limit, whatever the floats say
    assert changes["tagged"].regressed
    assert changes["tagged"].delta == pytest.approx(-0.06)


def test_a_new_metric_and_no_baseline_arent_regressions() -> None:
    changes = evals.compare(_record({"new": 0.1}), _record({"old": 1.0}))
    assert [(c.baseline, c.regressed) for c in changes] == [(None, False)]
    assert [c.regressed for c in evals.compare(_record({"a": 0.1}), None)] == [False]
