# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The core content index: what stages may write, how it's stored and queried."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from support import make_module, make_runtime

from steward.core import content
from steward.core.config import DiscoverySettings
from steward.core.db.schema import blobs, chunks, stage_runs
from steward.core.db.types import utcnow
from steward.core.discovery import Scanner
from steward.core.folders import store
from steward.core.search import fts_query, split_snippet
from steward.core.worker import Worker
from steward.sdk import MATCH_MARKERS, ChunkPart, Registry, StageContext, TextPart


def test_writes_replace_and_clean(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    start, end = MATCH_MARKERS
    with runtime.dbs.index.write() as conn:
        conn.execute(
            blobs.insert().values(
                content_hash="b3:x", hash_kind="full", size=1, mime="text/plain",
                kind="text", created_at=utcnow(),
            )
        )  # fmt: skip
        content.write_text(conn, "b3:x", "text", [TextPart(f"a{start}b{end}\x00c\ud800")])
        content.write_chunks(conn, "b3:x", "chunks", "text", [ChunkPart("one"), ChunkPart("  ")])
        assert content.read_text(conn, "b3:x", "text") == [TextPart("a b  c?")]
        content.write_chunks(conn, "b3:x", "chunks", "text", [ChunkPart("two"), ChunkPart("three")])
        assert [c.body for c in content.read_chunks(conn, "b3:x", "chunks")] == ["two", "three"]
        # The FTS index follows (old rows deleted by the triggers).
        rows = conn.exec_driver_sql(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'one'"
        ).all()
        assert rows == []
        assert (
            len(
                conn.exec_driver_sql(
                    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'three'"
                ).all()
            )
            == 1
        )
        # Deleting the blob takes its text and chunks with it.
        conn.execute(blobs.delete())
        assert conn.execute(select(chunks)).all() == []
    runtime.close()


def test_query_and_snippet_helpers() -> None:
    assert fts_query(["invoice", 'a"b', "-", "x*"]) == '"invoice" "a""b" "x*"'
    assert fts_query(["--", "…"]) is None
    start, end = MATCH_MARKERS
    assert split_snippet(f"the  {start}big{end} one\n…") == (
        ("the ", False),
        ("big", True),
        (" one …", False),
    )


def test_stages_may_only_write_what_they_produce(tmp_path: Path) -> None:
    def sneaky(ctx: StageContext) -> None:
        ctx.write_text([TextPart("x")], artifact="text:ocr")

    def register(r: Registry) -> None:
        r.stage("sneak.run", sneaky, version=1, requires=("mime",), produces=("text",))

    runtime = make_runtime(tmp_path, (make_module("sneak", register=register),))
    root = tmp_path / "lib"
    root.mkdir()
    (root / "a.txt").write_text("hello")
    with runtime.dbs.state.write() as conn:
        store.upsert_library(conn, "lib", root=str(root), actor="user:1")
    Scanner(runtime.dbs, DiscoverySettings()).scan("lib")
    Worker(runtime).drain()
    with runtime.dbs.index.read() as conn:
        run = conn.execute(
            select(stage_runs.c.status, stage_runs.c.error, stage_runs.c.content_hash).where(
                stage_runs.c.stage == "sneak.run"
            )
        ).one()
        assert content.text_of(conn, run.content_hash) == {}
    runtime.close()
    assert run.status == "failed"
    assert "doesn't produce artifact 'text:ocr'" in run.error
