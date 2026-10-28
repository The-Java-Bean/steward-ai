# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Discovery against real directory trees in tmp_path."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, update
from support import make_runtime

from steward.core.config import DiscoverySettings
from steward.core.db import utcnow
from steward.core.db.schema import blobs, events, files, folders, path_history, stage_runs
from steward.core.discovery import SCAN_JOB, ScanError, Scanner, Watcher
from steward.core.discovery.hashing import SAMPLE
from steward.core.folders import FolderRef, load_policy, store
from steward.core.runtime import Runtime
from steward.core.worker import Worker

ACTOR = "user:1"


class Env:
    def __init__(self, tmp_path: Path, **settings: Any) -> None:
        self.runtime: Runtime = make_runtime(tmp_path)
        # `.keep` holds each test library open (an empty root is refused) without
        # showing up in the counts.
        patterns = (*DiscoverySettings().ignore_patterns, ".keep")
        self.settings = DiscoverySettings(ignore_patterns=patterns, **settings)
        self.roots: dict[str, Path] = {}
        self.event_mark = 0

    def library(self, library_id: str, root: Path) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        with self.runtime.dbs.state.write() as conn:
            store.upsert_library(conn, library_id, root=str(root), actor=ACTOR)
        self.roots[library_id] = root
        return root

    def map(self, library_id: str, relpath: str, type_name: str) -> None:
        with self.runtime.dbs.state.write() as conn:
            store.set_type(conn, FolderRef(library_id, relpath), type_name, actor=ACTOR)

    def scan(self, library_id: str = "main", *paths: str) -> dict[str, int]:
        scanner = Scanner(self.runtime.dbs, self.settings)
        return {k: v for k, v in scanner.scan(library_id, paths or ("",)).as_dict().items() if v}

    def file(self, relpath: str, library_id: str = "main") -> Any:
        with self.runtime.dbs.state.read() as conn:
            return conn.execute(
                select(files).where(files.c.library_id == library_id, files.c.relpath == relpath)
            ).one_or_none()

    def all_files(self) -> dict[str, Any]:
        with self.runtime.dbs.state.read() as conn:
            return {f"{r.library_id}:{r.relpath}": r for r in conn.execute(select(files))}

    def new_events(self) -> list[tuple[str, dict[str, Any]]]:
        with self.runtime.dbs.state.read() as conn:
            rows = conn.execute(
                select(events.c.event_id, events.c.type, events.c.payload)
                .where(events.c.event_id > self.event_mark, events.c.type.like("file.%"))
                .order_by(events.c.event_id)
            ).all()
            self.event_mark = conn.execute(select(func.max(events.c.event_id))).scalar() or 0
        return [(r.type, r.payload) for r in rows]

    def type_at(self, relpath: str, library_id: str = "main") -> str:
        with self.runtime.dbs.state.read() as conn:
            return load_policy(conn).settings_for(FolderRef(library_id, relpath)).type.name


def write(path: Path, content: bytes | str = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    yield e
    e.runtime.close()


@pytest.fixture
def lib(env: Env, tmp_path: Path) -> Path:
    root = env.library("main", tmp_path / "lib")
    (root / ".keep").write_text("")  # tests empty the library; an empty root is refused
    return root


# --- basics --------------------------------------------------------------------------


def test_first_scan_finds_and_hashes_everything(env: Env, lib: Path) -> None:
    write(lib / "Docs" / "a.pdf", b"%PDF-1.4 one")
    write(lib / "Docs" / "deep" / "b.txt", "two")
    write(lib / "c.txt", "two")  # same content as b.txt: one blob, two files
    report = env.scan()
    assert report == {"files_new": 3, "folders_new": 2}
    found = env.all_files()
    assert set(found) == {"main:Docs/a.pdf", "main:Docs/deep/b.txt", "main:c.txt"}
    assert found["main:c.txt"].content_hash == found["main:Docs/deep/b.txt"].content_hash
    assert all(r.present and r.hash_kind == "full" for r in found.values())
    with env.runtime.dbs.index.read() as conn:
        mimes = {r.content_hash: r.mime for r in conn.execute(select(blobs))}
        hashed = conn.execute(select(func.count()).select_from(stage_runs)).scalar()
    assert len(mimes) == 2
    assert mimes[found["main:Docs/a.pdf"].content_hash] == "application/pdf"
    assert hashed == 2
    assert [t for t, _ in env.new_events()] == ["file.discovered"] * 3
    with env.runtime.dbs.state.read() as conn:
        present = conn.execute(select(folders.c.relpath).where(folders.c.present.is_(True)))
        assert set(present.scalars()) == {"", "Docs", "Docs/deep"}


def test_rescan_without_changes_does_nothing(env: Env, lib: Path) -> None:
    write(lib / "a.txt")
    env.scan()
    env.new_events()
    assert env.scan() == {"files_unchanged": 1}
    assert env.new_events() == []


def test_edit_keeps_identity(env: Env, lib: Path) -> None:
    path = write(lib / "a.txt", "one")
    env.scan()
    before = env.file("a.txt")
    env.new_events()
    write(path, "one, edited")
    assert env.scan() == {"files_changed": 1}
    after = env.file("a.txt")
    assert after.file_id == before.file_id
    assert after.content_hash != before.content_hash
    [(kind, payload)] = env.new_events()
    assert kind == "file.changed"
    assert payload["old_content_hash"] == before.content_hash


def test_missing_and_reappearing_keep_identity(env: Env, lib: Path) -> None:
    path = write(lib / "a.txt", "one")
    env.scan()
    file_id = env.file("a.txt").file_id
    moved_away = lib.parent / "elsewhere.txt"
    path.rename(moved_away)
    assert env.scan() == {"files_missing": 1}
    gone = env.file("a.txt")
    assert (gone.present, gone.missing_since is not None) == (False, True)
    moved_away.rename(path)
    assert env.scan() == {"files_reappeared": 1}
    assert env.file("a.txt").file_id == file_id
    assert env.file("a.txt").present
    assert [t for t, _ in env.new_events()][-2:] == ["file.missing", "file.reappeared"]


# --- moves -----------------------------------------------------------------------------


def test_rename_is_recognized_by_inode_without_rehashing(env: Env, lib: Path) -> None:
    write(lib / "a.txt", "content")
    env.scan()
    file_id = env.file("a.txt").file_id
    env.new_events()
    (lib / "sub").mkdir()
    (lib / "a.txt").rename(lib / "sub" / "renamed.txt")
    assert env.scan() == {"files_moved": 1, "folders_new": 1}
    assert env.file("sub/renamed.txt").file_id == file_id
    assert env.file("a.txt") is None
    [(kind, payload)] = env.new_events()
    assert (kind, payload["old_relpath"]) == ("file.moved", "a.txt")
    with env.runtime.dbs.state.read() as conn:
        history = conn.execute(select(path_history).where(path_history.c.file_id == file_id)).one()
    assert (history.old_relpath, history.new_relpath) == ("a.txt", "sub/renamed.txt")


def test_copy_then_delete_across_libraries_is_a_move_by_content(
    env: Env, lib: Path, tmp_path: Path
) -> None:
    other = env.library("other", tmp_path / "other")
    write(lib / "report.pdf", b"%PDF report")
    env.scan()
    file_id = env.file("report.pdf").file_id
    shutil.copy(lib / "report.pdf", other / "report.pdf")  # new inode
    (lib / "report.pdf").unlink()
    env.scan("main")
    assert env.scan("other") == {"files_moved": 1}
    moved = env.file("report.pdf", library_id="other")
    assert moved.file_id == file_id
    assert moved.present


def test_moves_within_one_scan_are_matched_before_marking_missing(env: Env, lib: Path) -> None:
    write(lib / "z_old" / "a.bin", os.urandom(100))
    env.scan()
    file_id = env.file("z_old/a.bin").file_id
    shutil.copytree(lib / "z_old", lib / "a_new")  # new inodes; scanned before z_old
    shutil.rmtree(lib / "z_old")
    report = env.scan()
    assert report["files_moved"] == 1
    assert "files_new" not in report
    assert env.file("a_new/a.bin").file_id == file_id


def test_a_long_missing_file_is_not_matched(env: Env, lib: Path) -> None:
    write(lib / "a.txt", "content")
    env.scan()
    old_id = env.file("a.txt").file_id
    (lib / "a.txt").unlink()
    env.scan()
    with env.runtime.dbs.state.write() as conn:
        conn.execute(update(files).values(missing_since=utcnow() - timedelta(days=30)))
    write(lib / "b.txt", "content")
    assert env.scan() == {"files_new": 1}
    assert env.file("b.txt").file_id != old_id


def test_duplicates_are_separate_files(env: Env, lib: Path) -> None:
    write(lib / "a.txt", "same")
    env.scan()
    shutil.copy(lib / "a.txt", lib / "b.txt")
    assert env.scan() == {"files_new": 1, "files_unchanged": 1}
    assert env.file("a.txt").file_id != env.file("b.txt").file_id


# --- folders ---------------------------------------------------------------------------


def test_renaming_a_mapped_folder_keeps_its_type_lock_and_files(env: Env, lib: Path) -> None:
    write(lib / "Private" / "taxes" / "2026.pdf", b"%PDF tax")
    env.map("main", "Private", "private")
    env.map("main", "Private/taxes", "documents")
    env.scan()
    file_id = env.file("Private/taxes/2026.pdf").file_id
    (lib / "Private").rename(lib / "Secret")
    report = env.scan()
    assert report["folders_moved"] == 1
    assert "files_missing" not in report
    assert env.type_at("Secret") == "private"
    assert env.type_at("Secret/taxes") == "documents"
    with env.runtime.dbs.state.read() as conn:
        settings = load_policy(conn).settings_for(FolderRef("main", "Secret/taxes/x"))
    assert settings.locked
    assert env.file("Secret/taxes/2026.pdf").file_id == file_id
    assert env.type_at("Private") == "general"


def test_a_reused_inode_does_not_hand_over_a_mapping(env: Env, lib: Path) -> None:
    write(lib / "Private" / "a.txt", "private")
    env.map("main", "Private", "private")
    env.scan()
    shutil.rmtree(lib / "Private")
    write(lib / "Public" / "b.txt", "public")
    # Pretend the filesystem reused the deleted folder's inode for the new one.
    with env.runtime.dbs.state.write() as conn:
        conn.execute(
            update(folders)
            .where(folders.c.relpath == "Private")
            .values(inode=os.stat(lib / "Public").st_ino)
        )
    report = env.scan()
    assert "folders_moved" not in report
    assert env.type_at("Public") == "general"


def test_mapped_folder_that_doesnt_exist_yet_gets_its_identity_when_created(
    env: Env, lib: Path
) -> None:
    env.map("main", "Later", "media")
    env.scan()
    write(lib / "Later" / "p.jpg", b"\xff\xd8\xff")
    env.scan()
    assert env.type_at("Later") == "media"
    with env.runtime.dbs.state.read() as conn:
        row = conn.execute(select(folders).where(folders.c.relpath == "Later")).one()
    assert row.present


# --- what's skipped --------------------------------------------------------------------


def test_skips_temp_files_ignored_types_type_ignore_lists_and_symlinks(env: Env, lib: Path) -> None:
    write(lib / "download.part")
    write(lib / "~$doc.docx")
    write(lib / "Code" / "app" / "main.py")
    write(lib / "Code" / "app" / "node_modules" / "m.js")
    write(lib / "Code" / "app" / ".git" / "HEAD")
    write(lib / "Hidden" / "secret.txt")
    write(lib / "real.txt")
    (lib / "link.txt").symlink_to(lib / "real.txt")
    env.map("main", "Code", "projects")
    env.map("main", "Hidden", "ignored")
    env.scan()
    assert set(env.all_files()) == {"main:Code/app/main.py", "main:real.txt"}


def test_mapping_a_folder_as_ignored_marks_its_files_missing(env: Env, lib: Path) -> None:
    write(lib / "Stuff" / "a.txt")
    env.scan()
    env.map("main", "Stuff", "ignored")
    assert env.scan() == {"files_missing": 1, "folders_missing": 1}


def test_sampled_hashing_follows_the_folder_type(env: Env, lib: Path) -> None:
    write(lib / "Games" / "big.iso", os.urandom(4 * SAMPLE))
    write(lib / "Games" / "small.cfg", "x")
    env.map("main", "Games", "games")
    env.scan()
    assert env.file("Games/big.iso").hash_kind == "sampled"
    assert env.file("Games/small.cfg").hash_kind == "full"
    # Changing the type to one that wants full hashes re-hashes it, same identity.
    file_id = env.file("Games/big.iso").file_id
    env.map("main", "Games", "documents")
    assert env.scan() == {"files_changed": 1, "files_unchanged": 1}
    assert env.file("Games/big.iso").hash_kind == "full"
    assert env.file("Games/big.iso").file_id == file_id


# --- safety -------------------------------------------------------------------------------


def test_an_empty_root_is_not_taken_as_everything_deleted(env: Env, lib: Path) -> None:
    write(lib / "a.txt")
    env.scan()
    (lib / "a.txt").unlink()
    (lib / ".keep").unlink()
    with pytest.raises(ScanError, match="is empty"):
        env.scan()
    assert env.file("a.txt").present
    report = Scanner(env.runtime.dbs, env.settings, allow_empty_root=True).scan("main")
    assert report.files_missing == 1


def test_a_missing_root_fails(env: Env, lib: Path) -> None:
    shutil.rmtree(lib)
    with pytest.raises(ScanError, match="isn't a directory"):
        env.scan()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_unreadable_folders_are_not_marked_missing(env: Env, lib: Path) -> None:
    write(lib / "locked" / "a.txt")
    write(lib / "b.txt")
    env.scan()
    (lib / "locked").chmod(0)
    try:
        report = env.scan()
    finally:
        (lib / "locked").chmod(0o755)
    assert report["errors"] == 1
    assert env.file("locked/a.txt").present


def test_scanning_one_path_leaves_the_rest_alone(env: Env, lib: Path) -> None:
    write(lib / "A" / "a.txt")
    write(lib / "B" / "b.txt")
    env.scan()
    (lib / "B" / "b.txt").unlink()
    write(lib / "A" / "new.txt", "new")
    assert env.scan("main", "A") == {"files_new": 1, "files_unchanged": 1}
    assert env.file("B/b.txt").present  # not looked at
    assert env.scan("main", "B/b.txt") == {"files_missing": 1}


# --- two layouts (Phase 1 acceptance) ------------------------------------------------------


@pytest.mark.parametrize(
    ("layout", "mappings", "receipts", "renamed"),
    [
        (
            "numbered",
            {"20-docs": "documents", "20-docs/receipts": "archive"},
            "20-docs/receipts",
            "20-docs/bills",
        ),
        (
            "flat",
            {"Documents": "documents", "Documents/Receipts": "archive"},
            "Documents/Receipts",
            "Documents/Bills",
        ),
    ],
)
def test_renaming_a_mapped_folder_on_disk_keeps_its_type(
    env: Env, lib: Path, layout: str, mappings: dict[str, str], receipts: str, renamed: str
) -> None:
    for relpath, type_name in mappings.items():
        env.map("main", relpath, type_name)
    write(lib / receipts / "2026" / "r.pdf", b"%PDF r")
    env.scan()
    (lib / receipts).rename(lib / renamed)
    env.scan()
    assert env.type_at(renamed) == "archive"
    assert env.type_at(f"{renamed}/2026") == "archive"
    assert env.file(f"{renamed}/2026/r.pdf").present


# --- jobs and the watcher ------------------------------------------------------------------


def test_reconcile_job_scans_every_library(env: Env, lib: Path, tmp_path: Path) -> None:
    other = env.library("other", tmp_path / "other")
    write(lib / "a.txt")
    write(other / "b.txt")
    with env.runtime.dbs.state.write() as conn:
        from steward.core import queue

        queue.enqueue(conn, "core.reconcile", lane="io")
    Worker(env.runtime).drain()
    assert set(env.all_files()) == {"main:.keep", "main:a.txt", "other:b.txt"}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_watcher_debounces_and_batches_paths(env: Env, lib: Path) -> None:
    clock = FakeClock()
    watcher = Watcher(env.runtime.dbs, DiscoverySettings(debounce_seconds=10), clock=clock)
    root = str(lib)
    watcher.path_changed("main", root, str(lib / "a.txt"))
    watcher.path_changed("main", root, str(lib / "dl.part"))  # ignored
    watcher.path_changed("main", root, str(lib.parent / "outside.txt"))  # not in the library
    clock.now += 8
    watcher.path_changed("main", root, str(lib / "a.txt"))  # resets the timer
    watcher.path_changed("main", root, str(lib / "sub" / "b.txt"))
    clock.now += 5
    assert watcher.flush() == 0
    clock.now += 6
    assert watcher.flush() == 1
    with env.runtime.dbs.state.read() as conn:
        from steward.core import queue

        [job] = queue.list_jobs(conn)
    assert job.type == SCAN_JOB
    assert job.payload == {"library_id": "main", "paths": ["a.txt", "sub/b.txt"]}
    assert job.priority > 0


def test_watcher_follows_libraries(env: Env, lib: Path, tmp_path: Path) -> None:
    watcher = Watcher(env.runtime.dbs, DiscoverySettings())
    watcher.refresh()
    assert set(watcher.watched) == {"main"}
    env.library("other", tmp_path / "other")
    watcher.refresh()
    assert set(watcher.watched) == {"main", "other"}
    watcher.observer.stop()


def test_a_library_added_while_the_worker_runs_is_scanned(env: Env, tmp_path: Path) -> None:
    worker = Worker(env.runtime)
    worker.housekeeping()  # the subscriber starts at the newest event
    root = env.library("late", tmp_path / "late")
    write(root / "a.txt")
    worker.drain()
    assert env.file("a.txt", "late") is not None
