# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`files_ui`: the built-in file manager at `/ui/files` and the Locked Folder at
`/ui/locked` (plan §9.7, §14.1).

Browse by folder or by kind, preview, download files and folders (zip), upload, and
change things: new folder, rename, move, delete (to the trash), lock and unlock. Every
change is a journaled action run as the logged-in owner, with an undo button right
after it. The Locked Folder is the same file manager in the locked scope, behind a
password check. Built only on `steward.sdk`, like any other module.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar
from urllib.parse import quote

from steward.modules.files_ui.routes import LOCKED_PAGE, PAGE, locked_router, router
from steward.sdk import EntryKind, ModuleConfig, ModuleKind, Registry

HERE = Path(__file__).parent


def link(path: str, kind: EntryKind) -> str:
    target = "/view" if kind == "file" else ""
    return f"{PAGE}{target}?path={quote(path, safe=':/')}"


class FilesUiModule:
    name: ClassVar[str] = "files_ui"
    version: ClassVar[str] = "0.2.0"
    kind: ClassVar[ModuleKind] = "feature"
    requires: ClassVar[tuple[str, ...]] = ()
    config_model: ClassVar[type[ModuleConfig] | None] = None

    def register(self, r: Registry) -> None:
        r.ui_page(
            "files_ui.page",
            router,
            path=PAGE,
            title="Files",
            templates=HERE / "templates",
            static=HERE / "static",
        )
        r.ui_page(
            "files_ui.locked",
            locked_router,
            path=LOCKED_PAGE,
            title="Locked Folder",
            templates=HERE / "templates",
            static=HERE / "static",
        )
        r.link_builder("files_ui.links", link)
