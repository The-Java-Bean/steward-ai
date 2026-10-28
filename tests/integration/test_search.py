# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Filename/metadata search, on two different layouts: locked, hidden and trash folders
never show up in normal search; the Locked Folder scope finds only locked files."""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from support import make_runtime

from steward.core.config import DiscoverySettings
from steward.core.discovery import Scanner
from steward.core.folders import FolderRef, load_policy, store
from steward.core.runtime import Runtime
from steward.core.search import SearchError, SearchFilters, search_files

ACTOR = "user:1"
LAYOUTS: dict[str, dict[str, Any]] = {
    "numbered": {
        "files": [
            "10-documents/Invoices/2025 invoice ACME.pdf",
            "10-documents/notes/invoice ideas.md",
            "20-media/photos/beach.jpg",
            "90-private/invoice secret.pdf",
            "95-system/cache/invoice cache.bin",
        ],
        "map": {"10-documents": "documents", "90-private": "private", "95-system": "ignored"},
    },
    "flat": {
        "files": [
            "Documents/Invoices/2025 invoice ACME.pdf",
            "Documents/notes/invoice ideas.md",
            "Photos/beach.jpg",
            "Private/invoice secret.pdf",
            "Apps/cache/invoice cache.bin",
        ],
        "map": {"Documents": "documents", "Private": "private", "Apps": "ignored"},
    },
}


class Env:
    def __init__(self, tmp_path: Path, layout: str) -> None:
        self.runtime: Runtime = make_runtime(tmp_path)
        self.root = tmp_path / "lib"
        spec = LAYOUTS[layout]
        for i, relpath in enumerate(spec["files"]):
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"%PDF-1.4 x" if relpath.endswith(".pdf") else b"x" * (i + 1))
            os.utime(path, ns=(10**18 + i, 10**18 + i))
        with self.runtime.dbs.state.write() as conn:
            store.upsert_library(conn, "lib", root=str(self.root), actor=ACTOR)
            for relpath, type_name in spec["map"].items():
                store.set_type(conn, FolderRef("lib", relpath), type_name, actor=ACTOR)
            store.ensure_default_roles(conn, actor=ACTOR)
        trash = self.root / ".steward/trash/2026-01-01"
        trash.mkdir(parents=True)
        (trash / "old invoice.pdf").write_bytes(b"%PDF-1.4 old")
        Scanner(self.runtime.dbs, DiscoverySettings()).scan("lib")

    def find(self, query: str, **kwargs: object) -> list[str]:
        with self.runtime.dbs.state.read() as conn:
            policy = load_policy(conn)
        results = search_files(self.runtime.dbs, policy, query, **kwargs)  # type: ignore[arg-type]
        return [h.name for h in results.hits]


@pytest.fixture(params=sorted(LAYOUTS))
def env(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path, request.param)
    yield e
    e.runtime.close()


def test_normal_search_hides_locked_hidden_and_trash(env: Env) -> None:
    assert env.find("invoice") == ["invoice ideas.md", "2025 invoice ACME.pdf"]
    assert env.find("INVOICE acme") == ["2025 invoice ACME.pdf"]
    assert env.find("secret") == []
    assert env.find("old") == []


def test_names_rank_before_folders_and_newer_first(env: Env) -> None:
    # "invoices" matches the folder of the first file and the name of none.
    names = env.find("invoice")
    assert names[0] == "invoice ideas.md"  # newer of the two name matches


def test_filters(env: Env) -> None:
    assert env.find("invoice", filters=SearchFilters(kind="document")) == ["2025 invoice ACME.pdf"]
    assert env.find("", filters=SearchFilters(kind="image")) == ["beach.jpg"]
    assert env.find("", filters=SearchFilters(min_size=100)) == []
    later = datetime.fromtimestamp(10**9 + 2, tz=UTC)
    assert "2025 invoice ACME.pdf" not in env.find(
        "invoice", filters=SearchFilters(modified_after=later)
    )
    with pytest.raises(SearchError):
        env.find("x", filters=SearchFilters(modified_after=datetime(2025, 1, 1)))
    with pytest.raises(SearchError):
        env.find(" ".join(["a"] * 11))


def test_locked_scope_finds_only_locked_files(env: Env) -> None:
    assert env.find("invoice", scope="locked") == ["invoice secret.pdf"]


def test_unicode_is_folded(env: Env) -> None:
    path = env.root / unicodedata.normalize("NFD", "Café menu.txt")
    path.write_text("x")
    Scanner(env.runtime.dbs, DiscoverySettings()).scan("lib")
    assert env.find("café") == [unicodedata.normalize("NFD", "Café menu.txt")]
    assert env.find("CAFÉ MENU") != []
