# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The setup wizard's steps, without HTTP (plan §6.4, docs/adr/0013).

1. `owner`: create the owner account (`Auth.create_owner`; needs the setup token).
2. `libraries`: add the folders mounted into the container.
3. `types`: review suggested types for each library's top-level folders.
4. `private`: choose (or create) a private folder, optionally locked permanently; or skip.
5. `roles`: trash and edit-backup folders (hidden defaults, or chosen folders).

Progress is kept in `settings_kv` (`setup.step`, `setup.finished`), so the wizard picks
up where it was left. The wizard only adds: it never changes a folder that already has
a type, so it can't loosen anything set up before (for example by `folders.yaml`).
Every step can be redone later from the command line (and Settings).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

from sqlalchemy import Connection

from steward.core import settings
from steward.core.config import Config
from steward.core.executor import ActionFailed, Executor
from steward.core.folders import FolderRef, Policy, Role, load_policy
from steward.core.folders import store as folder_store
from steward.core.folders.paths import PathError, check_library_id
from steward.core.folders.seed import ImportReport, import_seed
from steward.core.setup.suggest import Suggestion, suggest
from steward.sdk import ActionRefused

type Step = Literal["owner", "libraries", "types", "private", "roles", "done"]
STEPS: tuple[Step, ...] = get_args(Step.__value__)
PRIVATE_TYPE = "private"
_ROLE_TYPES: dict[Role, str] = {"trash": "trash", "edit_backups": "archive"}
SETUP_ROLES: tuple[Role, ...] = tuple(_ROLE_TYPES)


class SetupError(ValueError):
    """A wizard choice can't be applied; the message is shown to the owner."""


@dataclass(frozen=True, slots=True)
class Progress:
    step: Step
    """The step to show next."""
    finished: bool
    private_folder: str | None
    private_skipped: bool


def progress(conn: Connection, *, has_owner: bool) -> Progress:
    stored = settings.get_all(conn, "setup")
    finished = bool(stored.get("finished"))
    step: Step
    if not has_owner:
        step = "owner"
    elif finished:
        step = "done"
    else:
        raw = stored.get("step")
        step = raw if raw in STEPS and raw != "owner" else "libraries"
    return Progress(
        step, finished, stored.get("private_folder"), bool(stored.get("private_skipped"))
    )


def advance(conn: Connection, to: Step, *, actor: str) -> None:
    settings.set_value(conn, "setup.step", to, actor=actor)
    if to == "done":
        settings.set_value(conn, "setup.finished", True, actor=actor)


# --- libraries ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LibraryCandidate:
    path: str
    library_id: str
    name: str
    source: Literal["libraries.yaml", "mounted"]


def suggest_library_id(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9_-]+", "-", name.casefold()).strip("-_")
    base = re.sub(r"^[^a-z]+", "", base)[:56] or "library"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def library_candidates(config: Config, policy: Policy) -> list[LibraryCandidate]:
    """Folders that look like libraries and aren't added yet: entries in
    `libraries.yaml`, then each folder inside the configured `setup.library_dirs`."""
    roots = {_real(lib.root) for lib in policy.libraries.values()}
    taken = set(policy.libraries)
    found: list[LibraryCandidate] = []
    for library_id, entry in config.libraries.libraries.items():
        if library_id in taken or _real(entry.path) in roots:
            continue
        found.append(
            LibraryCandidate(entry.path, library_id, entry.name or library_id, "libraries.yaml")
        )
        taken.add(library_id)
        roots.add(_real(entry.path))
    for parent in config.steward.setup.library_dirs:
        try:
            subdirs = sorted(os.scandir(parent), key=lambda e: e.name)
        except OSError:
            continue
        for sub in subdirs:
            if sub.name.startswith(".") or not sub.is_dir(follow_symlinks=False):
                continue
            if _real(sub.path) in roots:
                continue
            library_id = suggest_library_id(sub.name, taken)
            taken.add(library_id)
            found.append(LibraryCandidate(sub.path, library_id, sub.name, "mounted"))
    return found


def _real(path: str) -> str:
    return os.path.realpath(path)


def add_library(
    conn: Connection, library_id: str, path: str, *, name: str | None, actor: str
) -> list[str]:
    """Add a library after checking its folder. Returns warnings."""
    try:
        check_library_id(library_id)
    except PathError as exc:
        raise SetupError(str(exc)) from exc
    if not os.path.isabs(path):
        raise SetupError(f"{path!r} isn't an absolute path")
    if not os.path.isdir(path):
        raise SetupError(f"{path} isn't a folder steward-ai can see (is it mounted?)")
    policy = load_policy(conn)
    if library_id in policy.libraries:
        raise SetupError(f"there's already a library called {library_id!r}")
    real = Path(_real(path))
    for lib in policy.libraries.values():
        other = Path(_real(lib.root))
        if real == other or real.is_relative_to(other) or other.is_relative_to(real):
            raise SetupError(f"{path} overlaps the library {lib.library_id!r} ({lib.root})")
    folder_store.upsert_library(conn, library_id, root=path, name=name or None, actor=actor)
    warnings = []
    if not os.access(path, os.W_OK):
        warnings.append(f"{path} isn't writable here, so steward-ai can't change anything in it")
    try:
        empty = not any(True for _ in os.scandir(path))
    except OSError:
        empty = False
    if empty:
        warnings.append(
            f"{path} is empty; if that's unexpected, check the mount (scans refuse empty roots)"
        )
    return warnings


def import_config_files(conn: Connection, config: Config, *, actor: str) -> ImportReport:
    """Apply `libraries.yaml` and `folders.yaml`, like `steward folders import`."""
    return import_seed(conn, libraries=config.libraries, folders=config.folders, actor=actor)


# --- folder types -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TopFolder:
    ref: FolderRef
    name: str
    current_type: str | None
    """The folder's own mapped type, if it has one (the wizard leaves those alone)."""
    effective_type: str
    locked: bool
    suggestion: Suggestion


def top_level_folders(policy: Policy, library_id: str) -> list[TopFolder]:
    """Folders directly inside a library root (not hidden ones), with suggestions."""
    library = policy.library(library_id)
    try:
        entries = sorted(os.scandir(library.root), key=lambda e: e.name.casefold())
    except OSError:
        return []
    found: list[TopFolder] = []
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
            continue
        try:
            ref = FolderRef.of(library_id, entry.name)
        except PathError:
            continue
        mapping = policy.mappings.get(ref)
        effective = policy.settings_for(ref)
        found.append(
            TopFolder(
                ref,
                entry.name,
                mapping.type_name if mapping else None,
                effective.type.name,
                effective.locked,
                suggest(Path(entry.path)),
            )
        )
    return found


def apply_types(conn: Connection, choices: dict[FolderRef, str], *, actor: str) -> list[FolderRef]:
    """Map each folder to its chosen type, skipping folders that already have one.
    Returns the folders that were mapped."""
    policy = load_policy(conn)
    unknown = sorted({t for t in choices.values() if t not in policy.types})
    if unknown:
        raise SetupError(f"unknown folder type {unknown[0]!r}")
    mapped = []
    for ref, type_name in sorted(choices.items()):
        policy.library(ref.library_id)
        existing = policy.mappings.get(ref)
        if existing is not None and existing.type_name is not None:
            continue
        folder_store.set_type(conn, ref, type_name, actor=actor)
        mapped.append(ref)
    return mapped


# --- private folder -----------------------------------------------------------------


def parse_new_folder(library_id: str, relpath: str) -> FolderRef:
    try:
        return FolderRef.of(library_id, relpath)
    except PathError as exc:
        raise SetupError(str(exc)) from exc


def choose_private(conn: Connection, ref: FolderRef, *, permanent: bool, actor: str) -> None:
    """Give `ref` (an existing folder) the `private` type (locked, local-only).
    `permanent`: only `steward folders unlock --permanent` on the host can ever unlock
    it. To make a new folder, call `create_private_folder` first."""
    policy = load_policy(conn)
    try:
        library = policy.library(ref.library_id)
    except PathError as exc:
        raise SetupError(str(exc)) from exc
    if not ref.relpath:
        raise SetupError("choose a folder inside the library, not the library itself")
    on_disk = os.path.join(library.root, ref.relpath)
    if not os.path.isdir(on_disk) or os.path.islink(on_disk):
        raise SetupError(f"{ref} isn't an existing folder")
    folder_store.set_type(conn, ref, PRIVATE_TYPE, actor=actor)
    if permanent:
        folder_store.lock(conn, ref, permanent=True, actor=actor)
    settings.set_value(conn, "setup.private_folder", str(ref), actor=actor)
    settings.set_value(conn, "setup.private_skipped", False, actor=actor)


def create_private_folder(executor: Executor, ref: FolderRef, *, actor: str) -> None:
    """Create a new folder for private files (mode 0700) through the executor, so it's
    journaled. Runs its own transaction: call it before `choose_private`, not inside one."""
    if not ref.relpath:
        raise SetupError("choose a folder inside the library, not the library itself")
    try:
        executor.run(
            "file.mkdir", {"path": str(ref), "parents": True, "private": True}, actor=actor
        )
    except ActionRefused as exc:
        raise SetupError(f"{exc}; choose an existing folder or another name") from None
    except ActionFailed as exc:
        raise SetupError(str(exc)) from None


def skip_private(conn: Connection, *, actor: str) -> None:
    settings.set_value(conn, "setup.private_skipped", True, actor=actor)


# --- roles --------------------------------------------------------------------------


def apply_roles(conn: Connection, chosen: dict[Role, FolderRef | None], *, actor: str) -> list[str]:
    """Give `trash`/`edit_backups` the chosen folders (None: the hidden default inside
    the first library). An unmapped chosen folder gets the role's usual type. Returns
    the roles that got the default."""
    for role, ref in chosen.items():
        if role not in _ROLE_TYPES:
            raise SetupError(f"unknown role {role!r}")
        if ref is None:
            continue
        policy = load_policy(conn)
        try:
            policy.library(ref.library_id)
        except PathError as exc:
            raise SetupError(str(exc)) from exc
        mapping = policy.mappings.get(ref)
        if mapping is None or mapping.type_name is None:
            folder_store.set_type(conn, ref, _ROLE_TYPES[role], actor=actor)
        try:
            folder_store.set_role(conn, role, ref, actor=actor)
        except folder_store.FolderError as exc:
            raise SetupError(str(exc)) from exc
    return [str(r) for r in folder_store.ensure_default_roles(conn, actor=actor)]


__all__ = [
    "SETUP_ROLES",
    "STEPS",
    "LibraryCandidate",
    "Progress",
    "SetupError",
    "Step",
    "TopFolder",
    "add_library",
    "advance",
    "apply_roles",
    "apply_types",
    "choose_private",
    "import_config_files",
    "library_candidates",
    "parse_new_folder",
    "progress",
    "skip_private",
    "suggest_library_id",
    "top_level_folders",
]
