# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Helpers for executor tests (importable as `executor_support`): a runtime with one
library built from `EXAMPLE_TREE`, scanned, and a folder outside it that must never
change."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import select
from support import make_runtime

from steward.core.config import DiscoverySettings
from steward.core.db.schema import files
from steward.core.discovery import Scanner
from steward.core.folders import FolderRef, store
from steward.core.runtime import Runtime
from steward.sdk import EXAMPLE_TREE

ACTOR = "user:1"


def build_tree(root: Path, tree: Mapping[str, str | None] = EXAMPLE_TREE) -> None:
    for relpath, content in tree.items():
        path = root / relpath
        if content is None:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("latin-1"))


def snapshot(root: Path, *, skip: tuple[str, ...] = (".steward",)) -> dict[str, Any]:
    """relpath -> content digest (files), "dir", or "link -> target"."""
    found: dict[str, Any] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        for name in [*dirnames, *filenames]:
            rel = os.path.normpath(os.path.join(rel_dir, name))
            if rel.split("/")[0] in skip:
                continue
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                found[rel] = f"link -> {os.readlink(path)}"
            elif os.path.isdir(path):
                found[rel] = "dir"
            else:
                with open(path, "rb") as f:
                    found[rel] = hashlib.sha256(f.read()).hexdigest()
    return found


class ExecEnv:
    def __init__(self, tmp_path: Path, **config: Any) -> None:
        self.runtime: Runtime = make_runtime(tmp_path, **config)
        self.root = tmp_path / "libs" / "lib"
        self.root.mkdir(parents=True)
        build_tree(self.root)
        self.outside = tmp_path / "outside"
        self.outside.mkdir()
        (self.outside / "keep.txt").write_text("outside\n")
        with self.runtime.dbs.state.write() as conn:
            store.upsert_library(conn, "lib", root=str(self.root), actor=ACTOR)
            store.ensure_default_roles(conn, actor=ACTOR)
        self.scan()

    @property
    def executor(self) -> Any:
        return self.runtime.executor

    def scan(self, *paths: str) -> None:
        Scanner(self.runtime.dbs, DiscoverySettings()).scan("lib", paths or ("",))

    def map(self, relpath: str, type_name: str) -> None:
        with self.runtime.dbs.state.write() as conn:
            store.set_type(conn, FolderRef("lib", relpath), type_name, actor=ACTOR)

    def lock(self, relpath: str, *, permanent: bool = False) -> None:
        with self.runtime.dbs.state.write() as conn:
            store.lock(conn, FolderRef("lib", relpath), permanent=permanent, actor=ACTOR)

    def file_row(self, relpath: str) -> Any:
        with self.runtime.dbs.state.read() as conn:
            return conn.execute(
                select(files).where(files.c.library_id == "lib", files.c.relpath == relpath)
            ).one_or_none()

    def outside_snapshot(self) -> dict[str, Any]:
        return snapshot(self.outside, skip=())

    def close(self) -> None:
        self.runtime.close()
