# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The executor end to end: trash, locked trash, identities following moves, edits with
backups, sensitivity, locks, journal and the rescans it queues. The generic safety
checks for every action type are in tests/executor/."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from executor_support import ACTOR, ExecEnv
from sqlalchemy import select

from steward.core.db import utcnow
from steward.core.db.schema import events, jobs, path_history
from steward.core.executor import SensitiveActionError
from steward.core.executor.guarded import content_hash
from steward.core.folders import FolderRef, load_policy, store
from steward.core.folders.policy import Policy
from steward.sdk import ActionRefused


@pytest.fixture
def env(tmp_path: Path) -> Iterator[ExecEnv]:
    e = ExecEnv(tmp_path)
    yield e
    e.close()


def _policy(env: ExecEnv) -> Policy:
    with env.runtime.dbs.state.read() as conn:
        return load_policy(conn)


def test_delete_moves_to_the_trash_and_keeps_identity(env: ExecEnv) -> None:
    before = env.file_row("docs/a.txt")
    with pytest.raises(SensitiveActionError):
        env.executor.run("file.delete", {"path": "lib:docs/a.txt"}, actor=ACTOR)
    entry = env.executor.run("file.delete", {"path": "lib:docs/a.txt"}, actor=ACTOR, confirmed=True)
    day = utcnow().strftime("%Y-%m-%d")
    trashed = f".steward/trash/{day}/docs/a.txt"
    assert not (env.root / "docs/a.txt").exists()
    assert (env.root / trashed).read_text() == "alpha\n"
    assert entry.sensitive
    assert entry.result["destination"] == f"lib:{trashed}"
    after = env.file_row(trashed)
    assert after.file_id == before.file_id
    assert env.file_row("docs/a.txt") is None
    with env.runtime.dbs.state.read() as conn:
        history = conn.execute(
            select(path_history).where(path_history.c.file_id == before.file_id)
        ).all()
        scan = conn.execute(select(jobs).where(jobs.c.type == "core.scan")).all()
        applied = conn.execute(select(events).where(events.c.type == "action.applied")).all()
    assert [(h.old_relpath, h.new_relpath) for h in history] == [("docs/a.txt", trashed)]
    assert scan[-1].priority == 20
    assert set(scan[-1].payload["paths"]) == {"docs/a.txt", trashed}
    assert applied[-1].payload["entry_id"] == entry.entry_id

    # Deleting the same name again picks a free name in the trash.
    (env.root / "docs/a.txt").write_text("again\n")
    second = env.executor.run(
        "file.delete", {"path": "lib:docs/a.txt"}, actor=ACTOR, confirmed=True
    )
    assert second.result["destination"] == f"lib:.steward/trash/{day}/docs/a (2).txt"

    env.executor.undo(entry.entry_id, actor=ACTOR, confirmed=True)
    assert env.file_row("docs/a.txt").file_id == before.file_id


def test_files_deleted_from_a_locked_folder_stay_locked_in_the_trash(env: ExecEnv) -> None:
    env.map("photos", "private")
    with pytest.raises(SensitiveActionError):
        env.executor.run(
            "file.move",
            {"source": "lib:photos/p.jpg", "destination": "lib:docs/p.jpg"},
            actor=ACTOR,
        )
    entry = env.executor.run(
        "file.delete", {"path": "lib:photos/p.jpg"}, actor=ACTOR, confirmed=True
    )
    destination = entry.result["destination"]
    assert destination.startswith("lib:.steward/trash/locked/")
    policy = _policy(env)
    settings = policy.settings_for(
        FolderRef("lib", destination.partition(":")[2].rsplit("/", 1)[0])
    )
    assert settings.locked
    # The rest of the trash isn't locked.
    assert not policy.settings_for(FolderRef("lib", ".steward/trash")).locked


def test_moving_a_folder_keeps_its_mapping_and_files(env: ExecEnv) -> None:
    env.map("docs", "documents")
    file_id = env.file_row("docs/nested/c.txt").file_id
    env.executor.run("file.rename", {"path": "lib:docs", "new_name": "papers"}, actor=ACTOR)
    policy = _policy(env)
    assert policy.mappings[FolderRef("lib", "papers")].type_name == "documents"
    assert env.file_row("papers/nested/c.txt").file_id == file_id
    with pytest.raises(ActionRefused, match="already exists"):
        env.executor.run("file.rename", {"path": "lib:papers", "new_name": "photos"}, actor=ACTOR)
    with pytest.raises(ActionRefused, match="into itself"):
        env.executor.run(
            "file.move",
            {"source": "lib:papers", "destination": "lib:papers/nested/x"},
            actor=ACTOR,
        )


def test_edit_keeps_a_backup_and_checks_the_expected_hash(env: ExecEnv) -> None:
    file_id = env.file_row("docs/a.txt").file_id
    with pytest.raises(ActionRefused, match="changed since it was read"):
        env.executor.run(
            "file.edit",
            {"path": "lib:docs/a.txt", "content": "x", "expected_hash": "blake3:00"},
            actor=ACTOR,
        )
    entry = env.executor.run(
        "file.edit",
        {
            "path": "lib:docs/a.txt",
            "content": "edited\n",
            "expected_hash": content_hash(b"alpha\n"),
        },
        actor=ACTOR,
    )
    backup = entry.result["backup"].partition(":")[2]
    assert backup.startswith(f".steward/backups/{utcnow():%Y/%m}/{file_id}/")
    assert (env.root / backup).read_text() == "alpha\n"
    assert (env.root / "docs/a.txt").read_text() == "edited\n"
    assert not list(env.root.rglob(".steward-tmp-*"))
    env.executor.undo(entry.entry_id, actor=ACTOR)
    assert (env.root / "docs/a.txt").read_text() == "alpha\n"


def test_locks_unlocks_and_permanent_locks(env: ExecEnv) -> None:
    lock = env.executor.run("folder.lock", {"path": "lib:docs"}, actor=ACTOR)
    assert not lock.sensitive
    assert _policy(env).settings_for(FolderRef("lib", "docs/nested")).locked
    with pytest.raises(ActionRefused, match="already locked"):
        env.executor.run("folder.lock", {"path": "lib:docs"}, actor=ACTOR)
    with pytest.raises(ActionRefused, match="because lib:docs is"):
        env.executor.run("folder.unlock", {"path": "lib:docs/nested"}, actor=ACTOR, confirmed=True)
    with pytest.raises(SensitiveActionError):
        env.executor.run("folder.unlock", {"path": "lib:docs"}, actor=ACTOR)
    unlock = env.executor.run("folder.unlock", {"path": "lib:docs"}, actor=ACTOR, confirmed=True)
    assert not _policy(env).settings_for(FolderRef("lib", "docs")).locked
    env.executor.undo(unlock.entry_id, actor=ACTOR, confirmed=True)  # locks again
    assert _policy(env).settings_for(FolderRef("lib", "docs")).locked

    env.lock("photos", permanent=True)
    with pytest.raises(ActionRefused, match="permanently locked"):
        env.executor.run("folder.unlock", {"path": "lib:photos"}, actor=ACTOR, confirmed=True)
    assert [e.action for e in env.executor.entries()] == [
        "folder.lock",
        "folder.unlock",
        "folder.lock",
    ]


def test_undoing_a_sensitive_action_needs_confirmation(env: ExecEnv) -> None:
    entry = env.executor.run("file.delete", {"path": "lib:docs/b.md"}, actor=ACTOR, confirmed=True)
    with pytest.raises(SensitiveActionError):
        env.executor.undo(entry.entry_id, actor=ACTOR)
    undo = env.executor.undo(entry.entry_id, actor=ACTOR, confirmed=True)
    assert undo.undo_of == entry.entry_id
    assert undo.inverse is not None
    redo = env.executor.undo(undo.entry_id, actor=ACTOR, confirmed=True)  # undo the undo
    assert not (env.root / "docs/b.md").exists()
    assert redo.undo_of == undo.entry_id


def test_other_libraries_get_their_own_trash(env: ExecEnv, tmp_path: Path) -> None:
    other = tmp_path / "libs" / "two"
    (other / "sub").mkdir(parents=True)
    (other / "sub" / "f.txt").write_text("f\n")
    with env.runtime.dbs.state.write() as conn:
        store.upsert_library(conn, "two", root=str(other), actor=ACTOR)
    entry = env.executor.run("file.delete", {"path": "two:sub/f.txt"}, actor=ACTOR, confirmed=True)
    assert entry.result["destination"].startswith("two:.steward/trash/")
    policy = _policy(env)
    assert policy.mappings[FolderRef("two", ".steward/trash")].type_name == "trash"


def test_unknown_actions_and_bad_parameters_are_refused(env: ExecEnv) -> None:
    with pytest.raises(ActionRefused, match="unknown action"):
        env.executor.run("file.shred", {"path": "lib:docs/a.txt"}, actor=ACTOR)
    with pytest.raises(ActionRefused, match="invalid parameters"):
        env.executor.run("file.move", {"source": "lib:docs/a.txt"}, actor=ACTOR)
    with pytest.raises(ActionRefused, match="invalid parameters"):
        env.executor.run("file.rename", {"path": "lib:docs/a.txt", "new_name": "../x"}, actor=ACTOR)
    with pytest.raises(ActionRefused, match="library root"):
        env.executor.run("file.delete", {"path": "lib:"}, actor=ACTOR, confirmed=True)
    env.map("docs", "ignored")
    with pytest.raises(ActionRefused, match="writable"):
        env.executor.run("file.create", {"path": "lib:docs/x.txt", "content": "x"}, actor=ACTOR)
    assert env.executor.entries() == []
