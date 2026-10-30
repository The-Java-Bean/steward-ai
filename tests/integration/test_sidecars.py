# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""XMP sidecars end to end with a fake exiftool, on two layouts: the job writes tags and
descriptions next to photos in folders whose type asks for it and nowhere else, merges with
what other programs wrote, journals one compact line per folder, and sidecars follow their
photos through moves, renames, trash and undo. Also the SDK handles it relies on."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from api_support import log_in, make_owner
from fastapi.testclient import TestClient
from photos_support import (
    exiftool_calls,
    fake_sidecar,
    make_fake_exiftool,
    make_jpeg,
    read_fake_sidecar,
)
from sqlalchemy import select
from support import eps, write_config

from steward.api import create_app
from steward.core import content, queue
from steward.core.config import DiscoverySettings, load_config
from steward.core.db.schema import files, jobs, journal
from steward.core.discovery import Scanner
from steward.core.folders import FolderRef, TypeDefinition, store
from steward.core.library import Library
from steward.core.runtime import Runtime, build_runtime
from steward.core.search import search_all
from steward.core.worker import Worker
from steward.modules.files_ui import FilesUiModule
from steward.modules.photos import PhotosModule
from steward.modules.photos.sidecars import ACTION, EMPTY_XMP, table
from steward.sdk import ActionRefused, JobContext, Registry, TextPart

ACTOR = "user:1"
JOB = "photos.sidecars"
LAYOUTS: dict[str, dict[str, str]] = {
    "numbered": {
        "media": "20-media",
        "docs": "10-docs",
        "private": "90-private",
        "app": "20-media/managed",
    },
    "flat": {"media": "Photos", "docs": "Documents", "private": "Vault", "app": "AppPhotos"},
}
TYPES = {"media": "media", "docs": "documents", "private": "private", "app": "app-managed"}


class Env:
    def __init__(self, tmp_path: Path, layout: str, **settings: Any) -> None:
        self.tmp_path = tmp_path
        self.spec = LAYOUTS[layout]
        self.root = tmp_path / "lib"
        self.fake = make_fake_exiftool(tmp_path / "bin")
        self.paths = {
            "beach": f"{self.spec['media']}/beach.jpg",
            "barn": f"{self.spec['media']}/barn.jpg",
            "other": f"{self.spec['media']}/sub/other.jpg",
            "doc": f"{self.spec['docs']}/scan.jpg",
            "secret": f"{self.spec['private']}/secret.jpg",
            "app": f"{self.spec['app']}/app.jpg",
        }
        for i, relpath in enumerate(self.paths.values()):
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(make_jpeg(color=(10 + i * 30, 120, 200)))
        modules = (PhotosModule, FilesUiModule)
        config_dir = write_config(
            tmp_path / "config",
            steward={"data_dir": str(tmp_path / "data")},
            modules={
                "modules": {
                    "files_ui": {"enabled": True},
                    "photos": {
                        "enabled": True,
                        "settings": {
                            "exif_command": str(self.fake),
                            "captions": False,
                            **settings,
                        },
                    },
                }
            },
        )
        self.runtime: Runtime = build_runtime(load_config(config_dir), entry_points=eps(*modules))
        self.runtime.upgrade_databases()
        with self.runtime.dbs.state.write() as conn:
            store.upsert_library(conn, "lib", root=str(self.root), actor=ACTOR)
            for key, type_name in TYPES.items():
                store.set_type(conn, FolderRef("lib", self.spec[key]), type_name, actor=ACTOR)
            store.ensure_default_roles(conn, actor=ACTOR)
        self.scan()

    def scan(self) -> None:
        Scanner(self.runtime.dbs, DiscoverySettings()).scan("lib")
        Worker(self.runtime).drain()

    def path(self, key: str) -> str:
        return f"lib:{self.paths[key]}"

    def sidecar(self, key: str) -> Path:
        return self.root / (self.paths[key] + ".xmp")

    def run(self, action: str, params: dict[str, Any], **kwargs: Any) -> Any:
        kwargs.setdefault("confirmed", True)
        return self.runtime.executor.run(action, params, actor=ACTOR, **kwargs)

    def tag(self, key: str, *names: str) -> None:
        for name in names:
            self.run("tag.add", {"path": self.path(key), "tag": name})

    def describe(self, key: str, text: str) -> None:
        with self.runtime.dbs.state.read() as conn:
            content_hash = conn.execute(
                select(files.c.content_hash).where(files.c.relpath == self.paths[key])
            ).scalar_one()
        with self.runtime.dbs.index.write() as conn:
            content.write_text(conn, content_hash, "text:caption", [TextPart(text)])

    def sync(self) -> None:
        with self.runtime.dbs.state.write() as conn:
            queue.enqueue(conn, JOB, lane="executor", dedupe_key=f"schedule:{JOB}")
        worker = Worker(self.runtime, lanes=("executor",))
        while worker.run_one("executor"):
            pass
        self.scan()

    def journal(self) -> list[Any]:
        with self.runtime.dbs.state.read() as conn:
            rows = conn.execute(select(journal).where(journal.c.action == ACTION)).all()
        return list(rows)


@pytest.fixture(params=list(LAYOUTS))
def env(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path, request.param)
    yield e
    e.runtime.close()


# --- writing ----------------------------------------------------------------------------


def test_tags_and_descriptions_reach_the_sidecar(env: Env) -> None:
    env.tag("beach", "places/beach", "trips")
    env.describe("beach", "A dog on a sandy beach.")
    env.sync()
    assert read_fake_sidecar(env.sidecar("beach")) == {
        "Description": "A dog on a sandy beach.",
        "HierarchicalSubject": ["places|beach", "trips"],
        "Subject": ["beach", "places", "trips"],
    }
    assert env.sidecar("beach").read_bytes().startswith(b"<?xpacket")


def test_a_photo_with_nothing_to_say_gets_no_sidecar(env: Env) -> None:
    env.tag("beach", "trips")
    env.sync()
    assert env.sidecar("beach").exists()
    assert not env.sidecar("barn").exists()


def test_only_folders_that_ask_for_sidecars_get_them(env: Env) -> None:
    for key in ("doc", "secret", "app", "beach"):
        env.tag(key, "trips")
    env.sync()
    assert env.sidecar("beach").exists()
    assert not env.sidecar("doc").exists()
    assert not env.sidecar("secret").exists()
    assert not env.sidecar("app").exists()


def test_a_type_can_turn_them_on_and_a_custom_type_inherits_them(env: Env) -> None:
    with env.runtime.dbs.state.write() as conn:
        store.define_type(
            conn, "scans", TypeDefinition(extends="documents", sidecars=True), actor=ACTOR
        )
        store.define_type(conn, "albums", TypeDefinition(extends="media"), actor=ACTOR)
        store.set_type(conn, FolderRef("lib", env.spec["docs"]), "scans", actor=ACTOR)
        store.set_type(conn, FolderRef("lib", env.spec["media"]), "albums", actor=ACTOR)
    env.tag("doc", "trips")
    env.tag("beach", "trips")
    env.sync()
    assert env.sidecar("doc").exists()
    assert env.sidecar("beach").exists()


def test_a_later_change_updates_the_sidecar_and_an_unchanged_one_isnt_touched(env: Env) -> None:
    env.tag("beach", "trips")
    env.sync()
    first = env.sidecar("beach").stat().st_mtime_ns
    calls = len(exiftool_calls(env.fake))
    env.sync()
    assert env.sidecar("beach").stat().st_mtime_ns == first
    assert len(exiftool_calls(env.fake)) == calls
    env.run("tag.remove", {"path": env.path("beach"), "tag": "trips"})
    env.tag("beach", "family")
    env.sync()
    assert read_fake_sidecar(env.sidecar("beach"))["Subject"] == ["family"]


def test_an_idle_library_isnt_compared_again(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.tag("beach", "trips")
    env.sync()  # writes, which is a change...
    env.sync()  # ...so this one compares everything once more
    passes = []
    original = Library.files

    def counted(self: Library, **kwargs: Any) -> Any:
        passes.append(1)
        return original(self, **kwargs)

    monkeypatch.setattr(Library, "files", counted)
    env.sync()
    assert passes == []
    env.tag("barn", "trips")
    env.sync()
    assert passes
    assert env.sidecar("barn").exists()


def test_a_run_that_stopped_at_its_limit_goes_on_next_time(tmp_path: Path) -> None:
    env = Env(tmp_path, "flat", sidecar_batch=1, sidecar_run_limit=1)
    try:
        env.tag("beach", "trips")
        env.tag("barn", "trips")
        env.sync()
        assert len(list((env.root / env.spec["media"]).glob("*.xmp"))) == 1
        env.sync()
        assert len(list((env.root / env.spec["media"]).glob("*.xmp"))) == 2
    finally:
        env.runtime.close()


def test_the_original_is_never_touched(env: Env) -> None:
    before = (env.root / env.paths["beach"]).read_bytes()
    env.tag("beach", "trips")
    env.sync()
    assert (env.root / env.paths["beach"]).read_bytes() == before


def test_a_new_sidecar_is_scanned_but_nothing_reads_it(env: Env) -> None:
    env.tag("beach", "trips")
    env.sync()
    with env.runtime.dbs.state.read() as conn:
        row = conn.execute(
            select(files).where(files.c.relpath == env.paths["beach"] + ".xmp")
        ).one()
    with env.runtime.dbs.index.read() as conn:
        assert content.text_of(conn, row.content_hash) == {}


# --- merging ----------------------------------------------------------------------------


def test_other_programs_sidecars_are_merged_not_overwritten(env: Env) -> None:
    env.sidecar("beach").write_bytes(
        fake_sidecar(Subject=["theirs"], Description="Their words", Rating=3)
    )
    env.scan()
    env.tag("beach", "mine")
    env.describe("beach", "My words")
    env.sync()
    assert read_fake_sidecar(env.sidecar("beach")) == {
        "Description": "Their words",
        "HierarchicalSubject": ["mine"],
        "Rating": 3,
        "Subject": ["theirs", "mine"],
    }
    env.run("tag.remove", {"path": env.path("beach"), "tag": "mine"})
    env.sync()
    assert read_fake_sidecar(env.sidecar("beach"))["Subject"] == ["theirs"]


def test_editing_the_sidecar_elsewhere_survives_the_next_sync(env: Env) -> None:
    env.tag("beach", "one")
    env.sync()
    record = read_fake_sidecar(env.sidecar("beach"))
    record["Rating"] = 5
    env.sidecar("beach").write_bytes(fake_sidecar(**record))
    env.scan()
    env.tag("beach", "two")
    env.sync()
    after = read_fake_sidecar(env.sidecar("beach"))
    assert after["Rating"] == 5
    assert after["Subject"] == ["one", "two"]


def test_a_sidecar_that_isnt_xmp_is_left_alone(env: Env) -> None:
    hostile = b"AT&TFORM a DjVu file named .xmp"
    env.sidecar("beach").write_bytes(hostile)
    env.sidecar("barn").write_bytes(b"<?xml version='1.0'?><!DOCTYPE x []><x/>")
    env.scan()
    env.tag("beach", "trips")
    env.tag("barn", "trips")
    env.sync()
    assert env.sidecar("beach").read_bytes() == hostile
    assert env.journal() == []
    assert not any(a.endswith(".xmp") for c in exiftool_calls(env.fake) for a in c["args"])


def test_a_symlink_where_the_sidecar_would_go_is_left_alone(env: Env) -> None:
    outside = env.tmp_path / "outside.xmp"
    outside.write_bytes(EMPTY_XMP)
    env.sidecar("beach").symlink_to(outside)
    env.tag("beach", "trips")
    env.sync()
    assert outside.read_bytes() == EMPTY_XMP
    assert env.journal() == []


def test_a_sidecar_exiftool_cant_read_is_skipped(env: Env) -> None:
    env.sidecar("beach").write_bytes(fake_sidecar(UNREADABLE=1))
    env.scan()
    env.tag("beach", "trips")
    env.tag("barn", "trips")
    env.sync()
    assert env.sidecar("barn").exists()
    assert "UNREADABLE" in read_fake_sidecar(env.sidecar("beach"))


def test_a_sidecar_exiftool_wouldnt_change_is_skipped(env: Env) -> None:
    env.sidecar("beach").write_bytes(fake_sidecar(NOEDIT=1))
    env.scan()
    env.tag("beach", "trips")
    env.sync()
    assert env.journal() == []
    with env.runtime.dbs.state.read() as conn:
        assert conn.execute(select(table)).all() == []


def test_exiftool_failing_postpones_the_job_and_changes_nothing(env: Env) -> None:
    env.tag("beach", "trips")
    env.sidecar("barn").write_bytes(fake_sidecar(Subject=["theirs"]))
    env.scan()
    env.tag("barn", "trips")
    (env.fake.parent / "BROKEN").touch()
    env.sync()
    assert read_fake_sidecar(env.sidecar("barn")) == {"Subject": ["theirs"]}
    with env.runtime.dbs.state.read() as conn:
        statuses = set(conn.execute(select(jobs.c.status).where(jobs.c.type == JOB)).scalars())
    assert statuses <= {"queued", "done"}
    assert "queued" in statuses


def test_a_sidecar_changed_after_it_was_read_is_skipped_and_merged_next_time(env: Env) -> None:
    env.tag("beach", "trips")
    env.tag("barn", "trips")
    folder = "lib:" + env.paths["beach"].rsplit("/", 1)[0]
    env.sidecar("beach").write_bytes(fake_sidecar(Subject=["theirs"]))
    entry = env.run(
        ACTION,
        {
            "folder": folder,
            "items": [
                {
                    "name": "beach.jpg.xmp",
                    "expected": None,
                    "content": EMPTY_XMP.decode(),
                    "file_id": "x",
                    "record": {},
                },
                {
                    "name": "barn.jpg.xmp",
                    "expected": None,
                    "content": EMPTY_XMP.decode(),
                    "file_id": "y",
                    "record": {},
                },
            ],
        },
    )
    assert entry.result["written"] == 1
    assert entry.result["skipped"] == 1
    assert read_fake_sidecar(env.sidecar("beach")) == {"Subject": ["theirs"]}


# --- the journal ------------------------------------------------------------------------


def test_one_compact_journal_line_per_folder_and_nothing_to_undo(env: Env) -> None:
    env.tag("beach", "trips")
    env.tag("barn", "trips")
    env.tag("other", "trips")
    env.describe("beach", "A very particular description.")
    env.sync()
    rows = env.journal()
    assert len(rows) == 2
    assert {r.actor for r in rows} == {"module:photos"}
    assert all(r.inverse is None for r in rows)
    text = json.dumps([[r.params, r.plan, r.result, r.summary] for r in rows])
    assert "particular description" not in text
    assert "<x:xmpmeta" not in text
    assert sorted(r.params["count"] for r in rows) == [1, 2]


def test_no_description_text_is_kept_in_state(env: Env) -> None:
    env.describe("beach", "A very particular description.")
    env.sync()
    with env.runtime.dbs.state.read() as conn:
        rows = conn.execute(select(table)).all()
    assert len(rows) == 1
    assert "particular" not in json.dumps(rows[0].data)


def test_big_folders_go_in_batches_and_a_run_stops_at_its_limit(tmp_path: Path) -> None:
    env = Env(tmp_path, "flat", sidecar_batch=2, sidecar_run_limit=3)
    try:
        folder = env.root / env.spec["media"]
        for i in range(6):
            shutil.copy(folder / "beach.jpg", folder / f"copy{i}.jpg")
            (folder / f"copy{i}.jpg").write_bytes(make_jpeg(color=(i * 20, 10, 10)))
        env.scan()
        for i in range(6):
            env.run("tag.add", {"path": f"lib:{env.spec['media']}/copy{i}.jpg", "tag": "trips"})
        env.sync()
        assert len(list(folder.glob("*.xmp"))) == 4
        assert {r.params["count"] for r in env.journal()} == {2}
        env.sync()
        assert len(list(folder.glob("*.xmp"))) == 6
    finally:
        env.runtime.close()


# --- sidecars follow their photos -------------------------------------------------------


def test_a_moved_photo_takes_its_sidecar_and_undo_brings_both_back(env: Env) -> None:
    env.tag("beach", "trips")
    env.sync()
    target = f"{env.spec['docs']}/beach.jpg"
    entry = env.run("file.move", {"source": env.path("beach"), "destination": f"lib:{target}"})
    assert not env.sidecar("beach").exists()
    assert (env.root / (target + ".xmp")).exists()
    env.runtime.executor.undo(entry.entry_id, actor=ACTOR, confirmed=True)
    assert env.sidecar("beach").exists()
    assert not (env.root / (target + ".xmp")).exists()


def test_a_renamed_photo_keeps_its_sidecar_and_the_next_sync_writes_nothing(env: Env) -> None:
    env.tag("beach", "trips")
    env.sync()
    lines = len(env.journal())
    env.run("file.rename", {"path": env.path("beach"), "new_name": "shore.jpg"})
    env.scan()
    assert not env.sidecar("beach").exists()
    renamed = env.sidecar("beach").with_name("shore.jpg.xmp")
    assert renamed.exists()
    env.sync()
    assert len(env.journal()) == lines
    env.tag("barn", "x")
    env.run("tag.add", {"path": f"lib:{env.spec['media']}/shore.jpg", "tag": "more"})
    env.sync()
    assert sorted(read_fake_sidecar(renamed)["Subject"]) == ["more", "trips"]


def test_moving_into_a_locked_folder_leaves_nothing_behind(env: Env) -> None:
    env.tag("beach", "trips")
    env.describe("beach", "A private description.")
    env.sync()
    target = f"{env.spec['private']}/beach.jpg"
    env.run("file.move", {"source": env.path("beach"), "destination": f"lib:{target}"})
    assert not env.sidecar("beach").exists()
    assert (env.root / (target + ".xmp")).exists()


def test_a_sidecar_already_at_the_destination_refuses_the_move(env: Env) -> None:
    env.tag("beach", "trips")
    env.sync()
    target = f"{env.spec['docs']}/beach.jpg"
    (env.root / (target + ".xmp")).write_bytes(EMPTY_XMP)
    with pytest.raises(ActionRefused, match="already exists"):
        env.run("file.move", {"source": env.path("beach"), "destination": f"lib:{target}"})
    assert (env.root / env.paths["beach"]).exists()
    assert env.sidecar("beach").exists()


def test_a_deleted_photo_takes_its_sidecar_to_the_trash_and_undo_restores_both(env: Env) -> None:
    env.tag("beach", "trips")
    env.sync()
    entry = env.run("file.delete", {"path": env.path("beach")})
    assert not env.sidecar("beach").exists()
    assert entry.result["sidecar"][1].endswith("beach.jpg.xmp")
    env.runtime.executor.undo(entry.entry_id, actor=ACTOR, confirmed=True)
    assert env.sidecar("beach").exists()


def test_a_moved_photo_keeps_its_file_identity_for_the_sidecar_too(env: Env) -> None:
    env.tag("beach", "trips")
    env.sync()
    env.scan()
    relpath = env.paths["beach"] + ".xmp"
    with env.runtime.dbs.state.read() as conn:
        before = conn.execute(
            select(files.c.file_id).where(files.c.relpath == relpath)
        ).scalar_one()
    env.run("file.rename", {"path": env.path("beach"), "new_name": "shore.jpg"})
    with env.runtime.dbs.state.read() as conn:
        after = conn.execute(select(files.c.relpath).where(files.c.file_id == before)).scalar_one()
    assert after.endswith("shore.jpg.xmp")


def test_moved_outside_steward_ai_a_new_sidecar_is_written(env: Env) -> None:
    env.tag("beach", "trips")
    env.sync()
    source = env.root / env.paths["beach"]
    moved = source.with_name("moved.jpg")
    os.rename(source, moved)
    env.scan()
    env.sync()
    assert read_fake_sidecar(moved.with_name("moved.jpg.xmp"))["Subject"] == ["trips"]


# --- views ------------------------------------------------------------------------------


def test_a_sidecar_next_to_its_photo_isnt_listed_or_found_but_a_lone_one_is(env: Env) -> None:
    env.tag("beach", "trips")
    env.sync()
    lone = env.root / env.spec["media"] / "lone.xmp"
    lone.write_bytes(EMPTY_XMP)
    env.scan()
    make_owner(env.runtime)
    with TestClient(create_app(env.runtime), follow_redirects=False) as client:
        log_in(client)
        page = client.get("/ui/files", params={"path": f"lib:{env.spec['media']}"})
    assert page.status_code == 200
    assert "beach.jpg.xmp" not in page.text
    assert "beach.jpg" in page.text
    assert "lone.xmp" in page.text
    with env.runtime.dbs.state.read() as conn:
        from steward.core.folders import load_policy

        policy = load_policy(conn)
    hits = search_all(env.runtime.dbs, policy, "beach").hits
    assert [h.name for h in hits] == ["beach.jpg"]
    assert [h.name for h in search_all(env.runtime.dbs, policy, "lone").hits] == ["lone.xmp"]


# --- the action on its own ----------------------------------------------------------------


def _item(name: str = "beach.jpg.xmp", **fields: Any) -> dict[str, Any]:
    return {
        "name": name,
        "expected": None,
        "content": EMPTY_XMP.decode(),
        "file_id": "x",
        "record": {},
        **fields,
    }


@pytest.mark.parametrize(
    "name",
    ["../beach.jpg.xmp", "sub/other.jpg.xmp", "beach.jpg", "..\\beach.jpg.xmp", "none.jpg.xmp"],
)
def test_the_action_writes_only_sidecars_of_files_in_the_folder(env: Env, name: str) -> None:
    folder = "lib:" + env.paths["beach"].rsplit("/", 1)[0]
    with pytest.raises(ActionRefused):
        env.run(ACTION, {"folder": folder, "items": [_item(name)]})


def test_the_action_writes_only_xmp(env: Env) -> None:
    folder = "lib:" + env.paths["beach"].rsplit("/", 1)[0]
    with pytest.raises(ActionRefused, match="isn't XMP"):
        env.run(ACTION, {"folder": folder, "items": [_item(content="#!/bin/sh")]})


@pytest.mark.parametrize("folder", ["lib:../outside", "lib:/etc", "nolib:x", "lib:a\x00b"])
def test_the_action_refuses_hostile_folders(env: Env, folder: str) -> None:
    with pytest.raises(ActionRefused):
        env.run(ACTION, {"folder": folder, "items": [_item()]})


def test_the_action_refuses_a_symlinked_folder(env: Env) -> None:
    outside = env.tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "beach.jpg").write_bytes(b"x")
    (env.root / "link").symlink_to(outside)
    with pytest.raises(ActionRefused):
        env.run(ACTION, {"folder": "lib:link", "items": [_item()]})
    assert not (outside / "beach.jpg.xmp").exists()


def test_the_action_cant_write_into_a_read_only_folder(env: Env) -> None:
    folder = "lib:" + env.paths["app"].rsplit("/", 1)[0]
    with pytest.raises(ActionRefused):
        env.run(ACTION, {"folder": folder, "items": [_item("app.jpg.xmp")]})
    assert not env.sidecar("app").exists()


def test_a_module_cant_write_into_a_locked_folder(env: Env) -> None:
    folder = "lib:" + env.paths["secret"].rsplit("/", 1)[0]
    with pytest.raises(ActionRefused, match="password"):
        env.runtime.executor.run(
            ACTION, {"folder": folder, "items": [_item("secret.jpg.xmp")]}, actor="module:photos"
        )
    assert not env.sidecar("secret").exists()


# --- the SDK handles ---------------------------------------------------------------------


def test_library_access_never_shows_locked_or_hidden_files(env: Env) -> None:
    env.tag("beach", "trips")
    env.describe("beach", "A dog.")
    (env.root / ".steward/trash").mkdir(parents=True, exist_ok=True)
    (env.root / ".steward/trash/old.jpg").write_bytes(make_jpeg())
    env.scan()
    library = Library(env.runtime.dbs, env.runtime.privacy)
    page = library.files(kind="image", texts=("text:caption",))
    assert page.next_after is None
    found = {f.path: f for f in page.files}
    assert env.path("beach") in found
    assert env.path("secret") not in found
    assert not any(".steward" in p for p in found)
    beach = found[env.path("beach")]
    assert [t.name for t in beach.tags] == ["trips"]
    assert beach.texts == {"text:caption": "A dog."}
    assert beach.folder.sidecars
    assert beach.folder.writable
    assert not found[env.path("app")].folder.writable


def test_library_access_pages(env: Env) -> None:
    library = Library(env.runtime.dbs, env.runtime.privacy)
    seen: list[str] = []
    after = None
    while True:
        page = library.files(kind="image", after=after, limit=2)
        seen += [f.path for f in page.files]
        after = page.next_after
        if after is None:
            break
    assert len(seen) == len(set(seen)) == 5


def test_library_access_reads_and_refuses_symlinks(env: Env) -> None:
    library = Library(env.runtime.dbs, env.runtime.privacy)
    found = library.read(env.path("beach"), max_bytes=1 << 20)
    assert found is not None
    assert found[1].startswith("full:") or ":" in found[1]
    assert library.read("lib:nothing.jpg", max_bytes=10) is None
    with pytest.raises(ActionRefused):
        library.read(env.path("beach"), max_bytes=10)
    (env.root / "link.jpg").symlink_to(env.root / env.paths["beach"])
    with pytest.raises(ActionRefused):
        library.read("lib:link.jpg", max_bytes=1 << 20)


def test_only_the_executor_lane_can_run_actions(tmp_path: Path) -> None:
    seen: list[JobContext] = []

    class Probe:
        name = "probe"
        version = "0.1.0"
        kind = "feature"
        requires = ()
        config_model = None

        def register(self, r: Registry) -> None:
            r.job("probe.io", seen.append, lane="io")
            r.job("probe.exec", seen.append, lane="executor")

    config_dir = write_config(
        tmp_path / "config",
        steward={"data_dir": str(tmp_path / "data")},
        modules={"modules": {"probe": {"enabled": True}}},
    )
    runtime = build_runtime(load_config(config_dir), entry_points=eps(Probe))
    runtime.upgrade_databases()
    try:
        with runtime.dbs.state.write() as conn:
            queue.enqueue(conn, "probe.io", lane="io")
            queue.enqueue(conn, "probe.exec", lane="executor")
        Worker(runtime, lanes=("io",)).run_one("io")
        Worker(runtime, lanes=("executor",)).run_one("executor")
    finally:
        runtime.close()
    assert seen[0].actions is None
    assert seen[0].library is not None
    assert seen[1].actions is not None
    with pytest.raises(ActionRefused):
        seen[1].actions.run("nothing.here", {})


def test_without_exiftool_there_is_no_job(tmp_path: Path) -> None:
    config_dir = write_config(
        tmp_path / "config",
        steward={"data_dir": str(tmp_path / "data")},
        modules={
            "modules": {
                "photos": {
                    "enabled": True,
                    "settings": {"exif_command": str(tmp_path / "missing"), "captions": False},
                }
            }
        },
    )
    runtime = build_runtime(load_config(config_dir), entry_points=eps(PhotosModule))
    try:
        assert JOB not in runtime.registrations.job_types()
        assert ACTION not in runtime.registrations.actions()
    finally:
        runtime.close()
