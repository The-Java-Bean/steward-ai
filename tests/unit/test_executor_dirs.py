# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

import os
from pathlib import Path

import pytest

from steward.core.executor import ExecutorError, create_folder


def test_creates_nested_folders(tmp_path: Path) -> None:
    assert create_folder(str(tmp_path), "a/b/c", mode=0o700) is True
    assert (tmp_path / "a" / "b" / "c").is_dir()
    assert (tmp_path / "a" / "b" / "c").stat().st_mode & 0o777 == 0o700
    assert create_folder(str(tmp_path), "a/b/c") is False


def test_refuses_symlinks_and_files(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "lib"
    root.mkdir()
    os.symlink(outside, root / "link")
    with pytest.raises(ExecutorError, match="symlink"):
        create_folder(str(root), "link/new")
    assert not (outside / "new").exists()
    (root / "file").write_text("x")
    with pytest.raises(ExecutorError, match="not a folder"):
        create_folder(str(root), "file/new")


def test_refuses_bad_paths(tmp_path: Path) -> None:
    with pytest.raises(ExecutorError):
        create_folder(str(tmp_path), "")
    with pytest.raises(ValueError, match=r"'\.\.'"):
        create_folder(str(tmp_path), "../escape")
    with pytest.raises(ExecutorError, match="can't be opened"):
        create_folder(str(tmp_path / "missing"), "x")
