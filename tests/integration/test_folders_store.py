# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from support import make_runtime

from steward.core.config import FoldersConfig, LibrariesConfig
from steward.core.db import Database, utcnow
from steward.core.db.schema import events, path_history
from steward.core.folders import (
    FolderError,
    FolderRef,
    FolderTypeError,
    PathError,
    PermanentLockError,
    TypeDefinition,
    TypeSettings,
    load_policy,
    store,
)
from steward.core.folders.seed import import_seed

ACTOR = "user:1"


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = make_runtime(tmp_path).dbs.state
    with database.write() as conn:
        store.upsert_library(conn, "main", root=str(tmp_path / "lib"), name="Files", actor=ACTOR)
    return database


def ref(relpath: str) -> FolderRef:
    return FolderRef("main", relpath)


def settings(db: Database, relpath: str) -> Any:
    with db.read() as conn:
        return load_policy(conn).settings_for(ref(relpath))


def event_types(db: Database) -> list[str]:
    with db.read() as conn:
        return list(conn.execute(select(events.c.type).order_by(events.c.event_id)).scalars())


# --- libraries -------------------------------------------------------------------


def test_library_add_update_and_validation(db: Database, tmp_path: Path) -> None:
    with db.write() as conn:
        assert not store.upsert_library(conn, "main", root="/elsewhere", actor=ACTOR)
        with pytest.raises(PathError, match="absolute"):
            store.upsert_library(conn, "second", root="relative/path", actor=ACTOR)
        with pytest.raises(PathError, match="library id"):
            store.upsert_library(conn, "Bad Id", root="/x", actor=ACTOR)
        policy = load_policy(conn)
    assert policy.libraries["main"].root == "/elsewhere"
    assert policy.libraries["main"].name == "Files"
    assert event_types(db) == ["library.added", "library.updated"]


def test_folders_need_a_known_library(db: Database) -> None:
    with db.write() as conn, pytest.raises(PathError, match="unknown library"):
        store.ensure_folder(conn, FolderRef("nope", "x"))


# --- mappings, locks -------------------------------------------------------------


def test_map_and_unmap(db: Database) -> None:
    with db.write() as conn:
        store.set_type(conn, ref("Docs"), "documents", actor=ACTOR)
        store.set_type(conn, ref("Docs"), "documents", actor=ACTOR)  # no-op
    assert settings(db, "Docs/x").type.name == "documents"
    with db.write() as conn:
        store.set_type(conn, ref("Docs"), None, actor=ACTOR)
        assert ref("Docs") not in load_policy(conn).mappings
        with pytest.raises(FolderTypeError, match="unknown folder type"):
            store.set_type(conn, ref("Docs"), "nope", actor=ACTOR)
    assert event_types(db).count("folder.type_changed") == 2


def test_permanent_lock_rules(db: Database) -> None:
    with db.write() as conn:
        store.set_type(conn, ref("Private"), "private", actor=ACTOR)
        store.lock(conn, ref("Private"), permanent=True, actor=ACTOR)
        store.set_type(conn, ref("Private"), "general", actor=ACTOR)  # type change keeps the lock
    s = settings(db, "Private/x")
    assert (s.locked, s.permanent) == (True, True)
    with db.write() as conn:
        with pytest.raises(PermanentLockError):
            store.unlock(conn, ref("Private"), actor=ACTOR)
        with pytest.raises(PermanentLockError):
            store.clear_lock_override(conn, ref("Private"), actor=ACTOR)
        with pytest.raises(FolderError, match="unlock that instead"):
            store.unlock(conn, ref("Private/x"), allow_permanent=True, actor=ACTOR)
        store.unlock(conn, ref("Private"), allow_permanent=True, actor=ACTOR)
    assert not settings(db, "Private/x").locked
    assert "folder.unlocked" in event_types(db)


def test_unlock_and_relock_a_private_type_folder(db: Database) -> None:
    with db.write() as conn:
        store.set_type(conn, ref("Private"), "private", actor=ACTOR)
        store.unlock(conn, ref("Private"), actor=ACTOR)
    s = settings(db, "Private/x")
    assert not s.locked
    assert s.sensitivity == "local-only"
    with db.write() as conn:
        store.clear_lock_override(conn, ref("Private"), actor=ACTOR)
    assert settings(db, "Private/x").locked


# --- folder identity -------------------------------------------------------------


def test_moving_a_folder_keeps_its_type_lock_role_and_children(db: Database) -> None:
    with db.write() as conn:
        store.set_type(conn, ref("Docs"), "documents", actor=ACTOR)
        store.set_type(conn, ref("Docs/Receipts"), "archive", actor=ACTOR)
        store.lock(conn, ref("Docs/Receipts/Old"), actor=ACTOR)
        store.set_role(conn, "inbox", ref("Docs/In"), actor=ACTOR)
        moved = store.move_folder(conn, ref("Docs"), "Paperwork", actor=ACTOR)
        policy = load_policy(conn)
        history = conn.execute(select(path_history.c.old_relpath, path_history.c.new_relpath)).all()
    assert moved == 4
    assert policy.mappings[ref("Paperwork")].type_name == "documents"
    assert policy.mappings[ref("Paperwork/Receipts")].type_name == "archive"
    assert policy.settings_for(ref("Paperwork/Receipts/Old/x")).locked
    assert policy.roles["inbox"] == ref("Paperwork/In")
    assert ref("Docs") not in policy.mappings
    assert ("Docs/Receipts", "Paperwork/Receipts") in history
    assert "folder.moved" in event_types(db)


def test_move_collisions_and_stale_destinations(db: Database) -> None:
    from sqlalchemy import update

    from steward.core.db.schema import folders

    with db.write() as conn:
        store.set_type(conn, ref("A"), "documents", actor=ACTOR)
        store.set_type(conn, ref("B"), "media", actor=ACTOR)  # stale (not present on disk)
        store.move_folder(conn, ref("A"), "B", actor=ACTOR)
        assert load_policy(conn).mappings[ref("B")].type_name == "documents"

        store.ensure_folder(conn, ref("C"))
        conn.execute(update(folders).where(folders.c.relpath == "C").values(present=True))
        with pytest.raises(FolderError, match="already exists"):
            store.move_folder(conn, ref("B"), "C", actor=ACTOR)
        with pytest.raises(FolderError, match="into itself"):
            store.move_folder(conn, ref("B"), "B/inner", actor=ACTOR)
        with pytest.raises(FolderError, match="root"):
            store.move_folder(conn, ref(""), "x", actor=ACTOR)


# --- types -----------------------------------------------------------------------


def test_custom_type_lifecycle(db: Database) -> None:
    with db.write() as conn:
        assert (
            store.define_type(conn, "receipts", TypeDefinition(extends="documents"), actor=ACTOR)
            == "created"
        )
        assert (
            store.define_type(conn, "receipts", TypeDefinition(extends="documents"), actor=ACTOR)
            == "unchanged"
        )
        store.define_type(conn, "scans", TypeDefinition(extends="receipts"), actor=ACTOR)
        store.set_type(conn, ref("R"), "receipts", actor=ACTOR)
        with pytest.raises(FolderTypeError, match="still used"):
            store.delete_type(conn, "receipts", actor=ACTOR)
        store.rename_type(conn, "receipts", "bills", actor=ACTOR)
        policy = load_policy(conn)
        assert policy.mappings[ref("R")].type_name == "bills"
        assert policy.types["scans"].chain == ("scans", "bills", "documents", "general")
        store.delete_type(conn, "scans", actor=ACTOR)
        store.set_type(conn, ref("R"), None, actor=ACTOR)
        store.delete_type(conn, "bills", actor=ACTOR)
        assert "bills" not in load_policy(conn).types


def test_builtin_type_rules(db: Database) -> None:
    with db.write() as conn:
        store.define_type(conn, "documents", TypeDefinition(hash="sampled"), actor=ACTOR)
        assert load_policy(conn).types["documents"].hash == "sampled"
        with pytest.raises(FolderTypeError, match="can't extend"):
            store.define_type(conn, "documents", TypeDefinition(extends="media"), actor=ACTOR)
        with pytest.raises(FolderTypeError, match="can't be renamed"):
            store.rename_type(conn, "documents", "docs", actor=ACTOR)
        with pytest.raises(FolderTypeError, match="can't be deleted"):
            store.delete_type(conn, "documents", actor=ACTOR)
        store.define_type(conn, "a", TypeDefinition(extends="general"), actor=ACTOR)
        store.define_type(conn, "b", TypeDefinition(extends="a"), actor=ACTOR)
        with pytest.raises(FolderTypeError, match="cycle"):
            store.define_type(conn, "a", TypeDefinition(extends="b"), actor=ACTOR)


def test_overrides_are_stored_as_settings_only(db: Database) -> None:
    with db.write() as conn:
        store.define_type(conn, "media", TypeDefinition(index="metadata"), actor=ACTOR)
    with db.read() as conn:
        assert load_policy(conn).types["media"].index == "metadata"
    assert TypeSettings(index="metadata").model_dump(exclude_none=True) == {"index": "metadata"}


# --- roles -----------------------------------------------------------------------


def test_roles_need_writable_unlocked_folders(db: Database) -> None:
    with db.write() as conn:
        store.set_type(conn, ref("Immich"), "app-managed", actor=ACTOR)
        store.set_type(conn, ref("Private"), "private", actor=ACTOR)
        with pytest.raises(FolderError, match="isn't writable"):
            store.set_role(conn, "trash", ref("Immich/trash"), actor=ACTOR)
        with pytest.raises(FolderError, match="locked"):
            store.set_role(conn, "inbox", ref("Private/in"), actor=ACTOR)
        store.set_role(conn, "edit_backups", ref("Private/backups"), actor=ACTOR)  # allowed
        store.set_role(conn, "inbox", ref("In"), actor=ACTOR)
        store.clear_role(conn, "inbox", actor=ACTOR)
        assert set(load_policy(conn).roles) == {"edit_backups"}


def test_default_roles_go_in_the_first_library(db: Database) -> None:
    with db.write() as conn:
        store.upsert_library(conn, "second", root="/second", actor=ACTOR)
        assert store.ensure_default_roles(conn, actor=ACTOR) == ["trash", "edit_backups"]
        assert store.ensure_default_roles(conn, actor=ACTOR) == []
        policy = load_policy(conn)
    assert policy.roles["trash"] == ref(".steward/trash")
    assert policy.roles["edit_backups"] == ref(".steward/backups")
    trash = policy.settings_for(ref(".steward/trash/2026-01-01/x"))
    assert (trash.type.name, trash.sensitivity, trash.shareable) == ("trash", "local-only", False)


# --- seed import -----------------------------------------------------------------


def seed(tmp_path: Path) -> tuple[LibrariesConfig, FoldersConfig]:
    libraries = LibrariesConfig.model_validate(
        {
            "config_version": 1,
            "libraries": {"home": {"path": str(tmp_path / "home"), "name": "Home"}},
        }
    )
    folders = FoldersConfig.model_validate(
        {
            "config_version": 1,
            "types": {
                "scans": {"extends": "receipts", "stages": {"add": ["ocr"]}},
                "receipts": {"extends": "documents", "fields": ["vendor"]},
                "documents": {"hash": "sampled"},
            },
            "mappings": {
                "home": {
                    "Documents": "documents",
                    "Documents/Receipts": "receipts",
                    "Private": {"type": "private", "permanent": True},
                    "Scans": {"type": "scans", "locked": True},
                }
            },
            "roles": {"inbox": "home:Inbox"},
        }
    )
    return libraries, folders


def test_import_seed_is_idempotent(db: Database, tmp_path: Path) -> None:
    libraries, folders = seed(tmp_path)
    with db.write() as conn:
        first = import_seed(conn, libraries=libraries, folders=folders, actor=ACTOR)
    count = len(event_types(db))
    with db.write() as conn:
        second = import_seed(conn, libraries=libraries, folders=folders, actor=ACTOR)
        policy = load_policy(conn)
    assert first.libraries_added == ["home"]
    assert first.types_defined == ["documents", "receipts", "scans"]
    assert first.default_roles == ["trash", "edit_backups"]  # in "main", the first library
    assert second.default_roles == []
    assert second.libraries_added == []
    assert second.types_defined == []
    assert len(event_types(db)) == count  # nothing changed, nothing emitted
    home = FolderRef("home", "Private/x")
    assert policy.settings_for(home).permanent
    assert policy.settings_for(FolderRef("home", "Scans")).locked
    assert policy.types["scans"].hash == "sampled"
    assert policy.roles["inbox"] == FolderRef("home", "Inbox")


def test_import_cannot_lift_a_permanent_lock(db: Database, tmp_path: Path) -> None:
    libraries, folders = seed(tmp_path)
    with db.write() as conn:
        import_seed(conn, libraries=libraries, folders=folders, actor=ACTOR)
    unlocking = FoldersConfig.model_validate(
        {"config_version": 1, "mappings": {"home": {"Private": {"locked": False}}}}
    )
    with db.write() as conn, pytest.raises(FolderError, match="permanently locked"):
        import_seed(conn, libraries=libraries, folders=unlocking, actor=ACTOR)
    contradictory = FoldersConfig.model_validate(
        {"config_version": 1, "mappings": {"home": {"X": {"locked": False, "permanent": True}}}}
    )
    with db.write() as conn, pytest.raises(FolderError, match="permanent lock needs"):
        import_seed(conn, libraries=libraries, folders=contradictory, actor=ACTOR)


def test_example_seed_files_import(db: Database, tmp_path: Path) -> None:
    import yaml

    examples = Path(__file__).parents[2] / "config"
    libraries = LibrariesConfig.model_validate(
        yaml.safe_load((examples / "libraries.example.yaml").read_text())
    )
    folders = FoldersConfig.model_validate(
        yaml.safe_load((examples / "folders.example.yaml").read_text())
    )
    with db.write() as conn:
        import_seed(conn, libraries=libraries, folders=folders, actor=ACTOR)
        policy = load_policy(conn)
    assert policy.settings_for(FolderRef("main", "Private/x")).permanent
    assert policy.settings_for(FolderRef("main", "Documents/Receipts/2026")).type.name == "receipts"


def test_moving_a_folder_moves_its_file_rows(db: Database) -> None:
    from steward.core.db.schema import files

    now = utcnow()
    with db.write() as conn:
        for relpath, present in (("Docs/a.txt", True), ("New/stale.txt", False)):
            conn.execute(
                files.insert().values(
                    file_id=relpath,
                    library_id="main",
                    relpath=relpath,
                    size=1,
                    mtime_ns=1,
                    present=present,
                    first_seen=now,
                    last_seen=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        store.move_folder(conn, ref("Docs"), "New", actor=ACTOR)
        rows = dict(conn.execute(select(files.c.file_id, files.c.relpath)).all())
    assert rows == {"Docs/a.txt": "New/a.txt"}  # the stale row at the destination is gone
