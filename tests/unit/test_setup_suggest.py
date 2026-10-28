# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

import os
from pathlib import Path

import pytest

from steward.core.setup import suggest, suggest_by_name


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Documents", "documents"),
        ("10-media", "media"),
        ("20 Photos", "media"),
        ("40-notes", "notes"),
        ("Project", "projects"),  # trailing s optional
        ("Personal", "private"),
        ("Downloads", "inbox"),
        ("Old_Stuff", None),
        ("Backups", "archive"),
        ("xyz", None),
    ],
)
def test_names(name: str, expected: str | None) -> None:
    assert suggest_by_name(name) == expected


def _files(root: Path, names: list[str]) -> Path:
    root.mkdir(parents=True)
    for name in names:
        (root / name).write_bytes(b"x")
    return root


def test_markers_win_over_file_kinds(tmp_path: Path) -> None:
    folder = _files(tmp_path / "stuff", ["a.jpg", "b.jpg"])
    (folder / ".git").mkdir()
    assert suggest(folder).type_name == "projects"
    notes = _files(tmp_path / "things", ["n.md"])
    (notes / ".obsidian").mkdir()
    assert suggest(notes).type_name == "notes"


def test_file_kinds(tmp_path: Path) -> None:
    assert suggest(_files(tmp_path / "a", ["1.jpg", "2.png", "3.mp4", "x.txt"])).type_name == (
        "media"
    )
    assert suggest(_files(tmp_path / "b", ["1.pdf", "2.docx", "3.txt"])).type_name == "documents"
    assert suggest(_files(tmp_path / "c", ["setup.iso", "tool.exe"])).type_name == "software"
    mixed = suggest(_files(tmp_path / "d", ["1.jpg", "2.pdf", "3.zip", "4.bin"]))
    assert mixed.type_name is None
    empty = tmp_path / "e"
    empty.mkdir()
    assert suggest(empty) == suggest(empty).__class__(None, "it's empty")


def test_symlinks_and_hidden_folders_are_not_followed(tmp_path: Path) -> None:
    elsewhere = _files(tmp_path / "elsewhere", ["1.jpg", "2.jpg", "3.jpg"])
    folder = _files(tmp_path / "f", ["a.pdf"])
    os.symlink(elsewhere, folder / "link")
    hidden = folder / ".cache"
    hidden.mkdir()
    for i in range(5):
        (hidden / f"{i}.jpg").write_bytes(b"x")
    assert suggest(folder).type_name == "documents"
