# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from steward.core.db import utcnow
from steward.core.stages import StageGraphError, build_graph
from steward.core.stages.planner import RunRecord, run_status
from steward.sdk import Registrations, Registry, Stage, StageContext, StageResult


def noop(ctx: StageContext) -> StageResult | None:
    return None


def stages(*specs: tuple[str, tuple[str, ...], tuple[str, ...]], **extra: Any) -> dict[str, Stage]:
    """(name, requires, produces) triples, plus the root `core.hash`."""
    result = {"core.hash": Stage("core.hash", 1, ("hash", "mime"), noop, lane="io")}
    for name, requires, produces in specs:
        result[name] = Stage(name, extra.get(name, 1), produces, noop, requires=requires)
    return result


def test_orders_producers_before_consumers() -> None:
    graph = build_graph(
        stages(
            ("docs.chunk", ("text",), ("chunks",)),
            ("search.embed", ("chunks",), ("embedding:default",)),
            ("docs.extract", ("mime",), ("text",)),
            ("photos.exif", ("mime",), ("exif",)),
        )
    )
    assert list(graph.stages) == [
        "core.hash",
        "docs.extract",
        "photos.exif",
        "docs.chunk",
        "search.embed",
    ]
    assert graph.upstream("docs.chunk") == ("docs.extract",)
    assert graph.downstream["docs.extract"] == ("docs.chunk", "search.embed")
    assert graph.downstream["core.hash"] == (
        "docs.extract",
        "photos.exif",
        "docs.chunk",
        "search.embed",
    )


def test_duplicate_producers_are_refused() -> None:
    with pytest.raises(StageGraphError, match="produced by both"):
        build_graph(stages(("a.x", ("mime",), ("text",)), ("b.y", ("mime",), ("text",))))


def test_a_module_cannot_produce_the_roots_artifacts() -> None:
    with pytest.raises(StageGraphError, match="'mime' is produced by both"):
        build_graph(stages(("a.x", (), ("mime",))))


def test_missing_requirements_are_refused() -> None:
    with pytest.raises(StageGraphError, match="requires 'caption'"):
        build_graph(stages(("a.x", ("caption",), ("tags",))))


def test_cycles_are_refused() -> None:
    with pytest.raises(StageGraphError, match="cycle: a.x, b.y"):
        build_graph(stages(("a.x", ("b",), ("a",)), ("b.y", ("a",), ("b",))))


def test_the_root_must_exist() -> None:
    with pytest.raises(StageGraphError, match="root stage"):
        build_graph({"a.x": Stage("a.x", 1, ("a",), noop)})


def test_stage_without_requirements_still_comes_after_the_root() -> None:
    graph = build_graph(stages(("a.first", (), ("a",))))
    assert list(graph.stages) == ["core.hash", "a.first"]


def test_folder_types_refer_to_stages_by_full_or_short_name() -> None:
    graph = build_graph(
        stages(("photos.exif", ("mime",), ("exif",)), ("docs.ocr", ("mime",), ("ocr",)))
    )
    assert graph.matching({"exif", "docs.ocr", "nope"}) == {"photos.exif", "docs.ocr"}


def test_signature_follows_versions_and_shape() -> None:
    spec = ("a.x", ("mime",), ("text",))
    base = build_graph(stages(spec)).signature
    assert build_graph(stages(spec)).signature == base
    assert build_graph(stages(spec, **{"a.x": 2})).signature != base
    assert build_graph(stages(spec, ("b.y", ("text",), ("t2",)))).signature != base


def test_run_status_tracks_versions_and_recomputed_inputs() -> None:
    graph = build_graph(
        stages(("a.x", ("mime",), ("text",)), ("b.y", ("text",), ("chunks",)), **{"a.x": 2})
    )
    t0 = utcnow()

    def at(seconds: int) -> datetime:
        return t0 + timedelta(seconds=seconds)

    root = RunRecord(1, "done", at(0))
    # Everything current.
    runs = {
        "core.hash": root,
        "a.x": RunRecord(2, "done", at(1)),
        "b.y": RunRecord(1, "done", at(2)),
    }
    assert run_status(graph, runs) == ({"core.hash", "a.x", "b.y"}, {"core.hash", "a.x", "b.y"})
    # a.x is from an old version: it and everything after it is outdated.
    runs["a.x"] = RunRecord(1, "done", at(1))
    assert run_status(graph, runs) == ({"core.hash"}, {"core.hash"})
    # a.x was recomputed after b.y ran: b.y is outdated.
    runs["a.x"] = RunRecord(2, "done", at(3))
    assert run_status(graph, runs) == ({"core.hash", "a.x"}, {"core.hash", "a.x"})
    # a skipped input is current but not usable.
    runs["a.x"] = RunRecord(2, "skipped", at(1))
    assert run_status(graph, runs) == ({"core.hash", "a.x"}, {"core.hash"})


# --- registry validation -----------------------------------------------------------


def registry() -> Registry:
    return Registry("mod", None, Registrations())


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"version": 0}, "version must be >= 1"),
        ({"lane": "executor"}, "lane must be one of"),
        ({"produces": ()}, "must produce at least one"),
        ({"produces": ("Bad Kind",)}, "bad artifact kind"),
        ({"requires": ("text",), "produces": ("text",)}, "requires an artifact kind it produces"),
        ({"index": "none"}, "index must be"),
    ],
)
def test_registry_validates_stages(kwargs: dict[str, Any], message: str) -> None:
    args: dict[str, Any] = {"version": 1, "produces": ("out",), **kwargs}
    with pytest.raises(ValueError, match=message):
        registry().stage("mod.s", noop, **args)


def test_registry_stores_stages_under_the_module_prefix() -> None:
    r = registry()
    r.stage("mod.s", noop, version=3, requires=("mime",), produces=("out", "out"))
    stage = r._registrations.stages()["mod.s"]
    assert (stage.version, stage.requires, stage.produces, stage.short_name) == (
        3,
        ("mime",),
        ("out",),
        "s",
    )
    with pytest.raises(ValueError, match="must start with 'mod.'"):
        r.stage("other.s", noop, version=1, produces=("o2",))
