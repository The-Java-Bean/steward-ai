# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""A library with locked folders, for the Locked Folder tests (importable as
`locked_support`)."""

from __future__ import annotations

from pathlib import Path

from api_support import make_owner
from sqlalchemy import update
from support import make_runtime

from steward.core.config import DiscoverySettings
from steward.core.db.schema import sessions
from steward.core.discovery import Scanner
from steward.core.folders import FolderRef, store
from steward.modules.files_ui import FilesUiModule

ACTOR = "user:1"
SECRET_NAME = "okapi-hidden.txt"
"""A file name inside a locked folder that no request ever names."""
SECRET_CONTENT = "ZEBRA-CONTENT-7f3a"
"""Content that only files in locked folders hold."""

TREE = {
    "docs/report.txt": "quarterly report\n",
    "docs/sub/deep.txt": "deep\n",
    "photos/pic.png": "\x89PNG\r\n\x1a\nfake",
    "private/diary.txt": f"{SECRET_CONTENT} diary\n",
    f"private/{SECRET_NAME}": f"{SECRET_CONTENT}\n",
    "vault/zebra-secret.txt": f"{SECRET_CONTENT} vault\n",
    f"vault/inner/{SECRET_NAME}": f"{SECRET_CONTENT} inner\n",
    "vault/inner/deeper/note.txt": f"{SECRET_CONTENT} deeper\n",
    "vault/own/kept.txt": f"{SECRET_CONTENT} own\n",
}
"""`private` has the `private` type; `vault` is locked by its own setting, and
`vault/own` has a lock of its own too."""


class LockedEnv:
    def __init__(self, tmp_path: Path) -> None:
        self.runtime = make_runtime(tmp_path, (FilesUiModule,))
        self.root = tmp_path / "lib"
        for relpath, content in TREE.items():
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("latin-1"))
        with self.runtime.dbs.state.write() as conn:
            store.upsert_library(conn, "lib", root=str(self.root), actor=ACTOR)
            store.set_type(conn, FolderRef("lib", "private"), "private", actor=ACTOR)
            store.lock(conn, FolderRef("lib", "vault"), actor=ACTOR)
            store.lock(conn, FolderRef("lib", "vault/own"), actor=ACTOR)
            store.ensure_default_roles(conn, actor=ACTOR)
        self.scan()
        make_owner(self.runtime)

    def scan(self) -> None:
        Scanner(self.runtime.dbs, DiscoverySettings()).scan("lib")

    def forget_reauth(self) -> None:
        """Make every session's last password check old (not recent any more)."""
        with self.runtime.dbs.state.write() as conn:
            conn.execute(update(sessions).values(reauth_at=None))
