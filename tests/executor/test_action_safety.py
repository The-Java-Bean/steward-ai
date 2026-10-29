# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Executor safety (plan §17), run against **every registered action type** using its
`examples()` on `EXAMPLE_TREE`: apply → undo round trips, precondition drift, hostile
paths in every `LibraryPath` field, symlinks in the way, collisions, read-only folders,
and undo refused after a later change. A new action type is covered by adding it."""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from executor_support import ACTOR, ExecEnv, snapshot
from support import make_runtime

from steward.core.executor import ActionFailed
from steward.sdk import ActionRefused, ActionType, PathField

NO_EXAMPLES = {
    "folder.unlock",
    "file.upload",
    "tag.remove",
    "tag.create",
    "tag.update",
    "tag.merge",
}
"""Action types that need setup the shared tree doesn't have, or touch no paths (the
tag vocabulary); tested elsewhere (tests/integration/test_tags.py)."""


def _action_types() -> dict[str, ActionType]:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        runtime = make_runtime(Path(tmp))
        try:
            return runtime.registrations.actions()
        finally:
            runtime.close()


ACTIONS = _action_types()
CASES = [
    pytest.param(name, i, example, id=f"{name}-{i}")
    for name, action in sorted(ACTIONS.items())
    for i, example in enumerate(action.examples())
]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[ExecEnv]:
    e = ExecEnv(tmp_path)
    yield e
    e.close()


def _path_fields(action: ActionType) -> list[str]:
    return [
        name
        for name, info in action.params.model_fields.items()
        if any(isinstance(m, PathField) for m in info.metadata)
        or any(isinstance(m, PathField) for arg in _args(info.annotation) for m in _meta(arg))
    ]


def _args(annotation: Any) -> tuple[Any, ...]:
    return getattr(annotation, "__args__", ()) or ()


def _meta(annotation: Any) -> tuple[Any, ...]:
    return getattr(annotation, "__metadata__", ()) or ()


def test_every_action_type_has_examples() -> None:
    missing = sorted(
        name
        for name, action in ACTIONS.items()
        if not action.examples() and name not in NO_EXAMPLES
    )
    assert missing == []
    assert {"file.move", "file.delete", "file.edit", "folder.lock"} <= set(ACTIONS)


@pytest.mark.parametrize(("name", "i", "example"), CASES)
def test_apply_then_undo_restores_everything(
    env: ExecEnv, name: str, i: int, example: dict[str, Any]
) -> None:
    before = snapshot(env.root)
    entry = env.executor.run(name, example, actor=ACTOR, confirmed=True)
    assert entry.action == name
    if entry.inverse is None:
        pytest.skip(f"{name} can't be undone")
    env.executor.undo(entry.entry_id, actor=ACTOR, confirmed=True)
    assert snapshot(env.root) == before
    assert env.executor.get(entry.entry_id).undone_by is not None
    with pytest.raises(ActionRefused, match="already undone"):
        env.executor.undo(entry.entry_id, actor=ACTOR, confirmed=True)


@pytest.mark.parametrize(("name", "i", "example"), CASES)
def test_undo_is_refused_after_a_later_change(
    env: ExecEnv, name: str, i: int, example: dict[str, Any]
) -> None:
    entry = env.executor.run(name, example, actor=ACTOR, confirmed=True)
    post = [p for p in entry.result["post"] if p["kind"] == "file"]
    if entry.inverse is None or not post:
        pytest.skip("nothing to tamper with")
    _lib, _, relpath = post[0]["path"].partition(":")
    target = env.root / relpath
    target.write_bytes(target.read_bytes() + b"tampered")
    os.utime(target, ns=(1, 1))
    with pytest.raises(ActionRefused, match="changed since"):
        env.executor.undo(entry.entry_id, actor=ACTOR, confirmed=True)


@pytest.mark.parametrize(("name", "i", "example"), CASES)
def test_precondition_drift_refuses(
    env: ExecEnv, name: str, i: int, example: dict[str, Any]
) -> None:
    preview = env.executor.preview(name, example)
    files = [p for p in preview.plan.preconditions if p.kind == "file"]
    if not files:
        pytest.skip("no file preconditions")
    _lib, _, relpath = files[0].path.partition(":")
    (env.root / relpath).write_bytes(b"changed underneath")
    before = snapshot(env.root)
    from steward.core.executor import Executor  # the private apply path with a stale plan

    executor: Executor = env.executor
    action = executor.action_type(name)
    from steward.core.executor.guarded import ReadOnlyView

    with executor.dbs.state.read() as conn:
        from steward.core.folders import load_policy

        policy = load_policy(conn)
    view = ReadOnlyView(policy, executor.dbs.state)
    with pytest.raises(ActionRefused, match="changed since"):
        executor._apply(action, preview.plan, view, actor=ACTOR, confirmed=True)
    assert snapshot(env.root) == before
    assert executor.entries() == []


HOSTILE = [
    "lib:../outside/keep.txt",
    "lib:docs/../../outside/x",
    "lib:/etc/passwd",
    "lib:docs/\x00evil",
    "lib:docs/" + "x" * 256,
    "lib:docs\\..\\..\\outside",
    "nolib:docs/a.txt",
    "../outside/keep.txt",
    "lib:docs/./a.txt",
    "lib:docs//a.txt",
    "lib:docs/a.txt/",
    "lib:docs/new\nline",
]


@pytest.mark.parametrize(("name", "i", "example"), CASES)
def test_hostile_paths_are_refused(
    env: ExecEnv, name: str, i: int, example: dict[str, Any]
) -> None:
    fields = _path_fields(ACTIONS[name])
    assert fields, f"{name} has no LibraryPath fields"
    before, outside = snapshot(env.root), env.outside_snapshot()
    for field in fields:
        if field not in example:
            continue
        for hostile in HOSTILE:
            params = dict(example)
            params[field] = [hostile] if isinstance(example[field], list) else hostile
            with pytest.raises(ActionRefused):
                env.executor.run(name, params, actor=ACTOR, confirmed=True)
    assert snapshot(env.root) == before
    assert env.outside_snapshot() == outside


@pytest.mark.parametrize(("name", "i", "example"), CASES)
def test_symlinked_folders_are_never_followed(
    env: ExecEnv, name: str, i: int, example: dict[str, Any]
) -> None:
    """Replace the top folder of every path in the example with a symlink to a folder
    outside the library: the action must refuse and nothing outside may change."""
    tops = set()
    for field in _path_fields(ACTIONS[name]):
        values = example.get(field)
        for value in values if isinstance(values, list) else [values]:
            if isinstance(value, str) and "/" in value:
                tops.add(value.partition(":")[2].split("/")[0])
    tops = {t for t in tops if (env.root / t).is_dir()}
    if not tops:
        pytest.skip("no folder in the paths")
    for top in tops:
        decoy = env.outside / top
        os.rename(env.root / top, decoy)
        os.symlink(decoy, env.root / top)
    outside = env.outside_snapshot()
    with pytest.raises(ActionRefused, match="symlink"):
        env.executor.run(name, example, actor=ACTOR, confirmed=True)
    assert env.outside_snapshot() == outside


@pytest.mark.parametrize(("name", "i", "example"), CASES)
def test_collisions_never_overwrite(
    env: ExecEnv, name: str, i: int, example: dict[str, Any]
) -> None:
    preview = env.executor.preview(name, example)
    absent = [p.path for p in preview.plan.preconditions if p.kind == "absent"]
    if not absent:
        pytest.skip("creates nothing new")
    _, _, relpath = absent[0].partition(":")
    (env.root / relpath).parent.mkdir(parents=True, exist_ok=True)
    (env.root / relpath).write_text("already here\n")
    before = snapshot(env.root)
    try:
        env.executor.run(name, example, actor=ACTOR, confirmed=True)
        refused = ""
    except ActionRefused as exc:
        refused = str(exc)
    if refused:
        assert "exists" in refused or "changed since" in refused
        assert snapshot(env.root) == before
    # Refused, or a free name was chosen (trash, backups): never overwritten.
    assert (env.root / relpath).read_text() == "already here\n"


@pytest.mark.parametrize(("name", "i", "example"), CASES)
def test_read_only_folders_are_never_written(
    env: ExecEnv, name: str, i: int, example: dict[str, Any]
) -> None:
    preview = env.executor.preview(name, example)
    if not preview.plan.writes:
        pytest.skip("writes no files")
    env.map("", "app-managed")  # the whole library is read-only now
    before = snapshot(env.root, skip=())
    with pytest.raises(ActionRefused, match="writable"):
        env.executor.run(name, example, actor=ACTOR, confirmed=True)
    assert snapshot(env.root, skip=()) == before


@pytest.mark.parametrize(("name", "i", "example"), CASES)
def test_new_names_are_nfc(env: ExecEnv, name: str, i: int, example: dict[str, Any]) -> None:
    """A name typed in NFD is created in NFC."""
    preview = env.executor.preview(name, example)
    absent = [p.path for p in preview.plan.preconditions if p.kind == "absent"]
    fields = [f for f in _path_fields(ACTIONS[name]) if example.get(f) in absent]
    if not fields:
        pytest.skip("no new path among the parameters")
    params = dict(example)
    params[fields[0]] = example[fields[0]] + unicodedata.normalize("NFD", "-é")
    try:
        env.executor.run(name, params, actor=ACTOR, confirmed=True)
    except (ActionRefused, ActionFailed):
        pytest.skip("the changed name isn't valid for this example")
    names = {p.name for p in env.root.rglob("*")}
    assert not any(unicodedata.normalize("NFC", n) != n for n in names)
