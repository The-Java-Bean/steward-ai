# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The setup wizard's steps without HTTP (core.setup)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from support import make_runtime

from steward.core import setup
from steward.core.folders import FolderRef, load_policy
from steward.core.folders import store as folder_store
from steward.core.runtime import Runtime

ACTOR = "user:1"


@pytest.fixture
def libs(tmp_path: Path) -> Path:
    parent = tmp_path / "libraries"
    for name in ("Main Files", "second", ".hidden"):
        (parent / name).mkdir(parents=True)
    (parent / "Main Files" / "Documents").mkdir()
    (parent / "Main Files" / "Documents" / "a.pdf").write_bytes(b"%PDF")
    (parent / "Main Files" / "Private").mkdir()
    (parent / "Main Files" / ".cache").mkdir()
    os.symlink(parent / "second", parent / "Main Files" / "link")
    return parent


@pytest.fixture
def runtime(tmp_path: Path, libs: Path) -> Iterator[Runtime]:
    rt = make_runtime(
        tmp_path,
        steward={"setup": {"library_dirs": [str(libs), str(tmp_path / "missing")]}},
        libraries={"libraries": {"seeded": {"path": str(tmp_path / "seeded")}}},
    )
    yield rt
    rt.close()


def _policy(runtime: Runtime) -> object:
    with runtime.dbs.state.read() as conn:
        return load_policy(conn)


def _add(runtime: Runtime, library_id: str, path: Path) -> list[str]:
    with runtime.dbs.state.write() as conn:
        return setup.add_library(conn, library_id, str(path), name=None, actor=ACTOR)


def test_progress(runtime: Runtime) -> None:
    with runtime.dbs.state.write() as conn:
        assert setup.progress(conn, has_owner=False).step == "owner"
        assert setup.progress(conn, has_owner=True).step == "libraries"
        setup.advance(conn, "private", actor=ACTOR)
        assert setup.progress(conn, has_owner=True).step == "private"
        setup.advance(conn, "done", actor=ACTOR)
        done = setup.progress(conn, has_owner=True)
    assert (done.step, done.finished) == ("done", True)


def test_library_candidates(runtime: Runtime, libs: Path, tmp_path: Path) -> None:
    with runtime.dbs.state.read() as conn:
        found = setup.library_candidates(runtime.config, load_policy(conn))
    assert [(c.library_id, c.source) for c in found] == [
        ("seeded", "libraries.yaml"),
        ("main-files", "mounted"),
        ("second", "mounted"),
    ]
    _add(runtime, "main", libs / "Main Files")
    with runtime.dbs.state.read() as conn:
        found = setup.library_candidates(runtime.config, load_policy(conn))
    assert [c.library_id for c in found] == ["seeded", "second"]


def test_suggest_library_id() -> None:
    assert setup.suggest_library_id("My Files!", set()) == "my-files"
    assert setup.suggest_library_id("2024", set()) == "library"
    assert setup.suggest_library_id("main", {"main", "main-2"}) == "main-3"


def test_add_library_checks(runtime: Runtime, libs: Path, tmp_path: Path) -> None:
    with pytest.raises(setup.SetupError, match="absolute"):
        _add(runtime, "a", Path("relative"))
    with pytest.raises(setup.SetupError, match="isn't a folder"):
        _add(runtime, "a", tmp_path / "nope")
    with pytest.raises(setup.SetupError, match="library id"):
        _add(runtime, "Bad Id", libs / "second")
    assert _add(runtime, "main", libs / "Main Files") == []
    with pytest.raises(setup.SetupError, match="already a library"):
        _add(runtime, "main", libs / "second")
    with pytest.raises(setup.SetupError, match="overlaps"):
        _add(runtime, "inner", libs / "Main Files" / "Documents")
    with pytest.raises(setup.SetupError, match="overlaps"):
        _add(runtime, "outer", libs)
    warnings = _add(runtime, "second", libs / "second")
    assert any("empty" in w for w in warnings)


def test_top_level_folders_and_types(runtime: Runtime, libs: Path) -> None:
    _add(runtime, "main", libs / "Main Files")
    with runtime.dbs.state.write() as conn:
        folder_store.set_type(conn, FolderRef("main", "Private"), "archive", actor="test")
    with runtime.dbs.state.read() as conn:
        top = setup.top_level_folders(load_policy(conn), "main")
    # Hidden folders and symlinks aren't offered.
    assert [(f.name, f.current_type, f.suggestion.type_name) for f in top] == [
        ("Documents", None, "documents"),
        ("Private", "archive", "private"),
    ]
    with runtime.dbs.state.write() as conn:
        mapped = setup.apply_types(
            conn,
            {FolderRef("main", "Documents"): "documents", FolderRef("main", "Private"): "general"},
            actor=ACTOR,
        )
        with pytest.raises(setup.SetupError, match="unknown folder type"):
            setup.apply_types(conn, {FolderRef("main", "Documents"): "nope"}, actor=ACTOR)
    # The wizard only adds: the already-mapped folder keeps its type.
    assert mapped == [FolderRef("main", "Documents")]
    with runtime.dbs.state.read() as conn:
        policy = load_policy(conn)
    assert policy.mappings[FolderRef("main", "Private")].type_name == "archive"


def test_private_folder(runtime: Runtime, libs: Path) -> None:
    _add(runtime, "main", libs / "Main Files")
    ref = FolderRef("main", "Private")
    with runtime.dbs.state.write() as conn:
        with pytest.raises(setup.SetupError, match="inside the library"):
            setup.choose_private(conn, FolderRef("main", ""), permanent=False, actor=ACTOR)
        with pytest.raises(setup.SetupError, match="isn't an existing folder"):
            setup.choose_private(conn, FolderRef("main", "Nope"), permanent=False, actor=ACTOR)
        with pytest.raises(setup.SetupError, match="isn't an existing folder"):
            setup.choose_private(conn, FolderRef("main", "link"), permanent=False, actor=ACTOR)
        setup.choose_private(conn, ref, permanent=False, actor=ACTOR)
        progress = setup.progress(conn, has_owner=True)
    assert (progress.private_folder, progress.private_skipped) == ("main:Private", False)
    with runtime.dbs.state.read() as conn:
        settings = load_policy(conn).settings_for(ref)
    assert (settings.type.name, settings.locked, settings.permanent) == ("private", True, False)

    new = setup.parse_new_folder("main", "Vault/Inner")
    with pytest.raises(setup.SetupError, match="already exists"):
        setup.create_private_folder(runtime.executor, ref, actor=ACTOR)
    setup.create_private_folder(runtime.executor, new, actor=ACTOR)
    with runtime.dbs.state.write() as conn:
        setup.choose_private(conn, new, permanent=True, actor=ACTOR)
    assert (libs / "Main Files" / "Vault" / "Inner").is_dir()
    assert (libs / "Main Files" / "Vault" / "Inner").stat().st_mode & 0o777 == 0o700
    assert [e.action for e in runtime.executor.entries()] == ["file.mkdir"]
    with runtime.dbs.state.read() as conn:
        settings = load_policy(conn).settings_for(new)
    assert settings.permanent
    with pytest.raises(setup.SetupError):
        setup.parse_new_folder("main", "../escape")


def test_skip_private(runtime: Runtime) -> None:
    with runtime.dbs.state.write() as conn:
        setup.skip_private(conn, actor=ACTOR)
        assert setup.progress(conn, has_owner=True).private_skipped


def test_roles(runtime: Runtime, libs: Path) -> None:
    _add(runtime, "main", libs / "Main Files")
    chosen = FolderRef("main", "Documents")
    with runtime.dbs.state.write() as conn:
        defaults = setup.apply_roles(conn, {"trash": chosen, "edit_backups": None}, actor=ACTOR)
    assert defaults == ["edit_backups"]
    with runtime.dbs.state.read() as conn:
        policy = load_policy(conn)
    assert policy.roles["trash"] == chosen
    assert policy.mappings[chosen].type_name == "trash"
    assert policy.roles["edit_backups"] == FolderRef("main", ".steward/backups")
    with runtime.dbs.state.write() as conn, pytest.raises(setup.SetupError):
        setup.apply_roles(conn, {"trash": FolderRef("nope", "x")}, actor=ACTOR)


def test_import_config_files(runtime: Runtime, tmp_path: Path) -> None:
    (tmp_path / "seeded").mkdir()
    with runtime.dbs.state.write() as conn:
        report = setup.import_config_files(conn, runtime.config, actor=ACTOR)
    assert report.libraries_added == ["seeded"]
