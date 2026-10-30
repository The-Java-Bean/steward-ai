# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Stages calling chat roles through `StageContext.model` (structured output, provenance,
privacy refusals, waiting for the server), `queue.postpone`, and `steward models`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from model_server import FakeModelServer, llm_config, model_server
from pydantic import BaseModel
from sqlalchemy import select
from support import make_module, make_runtime, write_config
from typer.testing import CliRunner

from steward.cli import app
from steward.core import queue
from steward.core.config import DiscoverySettings
from steward.core.db import utcnow
from steward.core.db.schema import files, jobs, stage_runs
from steward.core.discovery import Scanner
from steward.core.folders import store
from steward.core.llm import PrivacyViolation
from steward.core.runtime import Runtime
from steward.core.worker import Worker
from steward.sdk import Registry, StageContext, StageResult

ACTOR = "user:1"
SEEN: list[dict[str, Any]] = []


class Summary(BaseModel):
    topic: str


def register_summaries(r: Registry) -> None:
    r.model_role("summary", kind="chat", description="Summarizes a file")

    def summarize(ctx: StageContext) -> StageResult:
        with ctx.open() as fh:
            text = fh.read().decode()
        model = ctx.model("summary")
        if "PRIVACY" in text:
            raise PrivacyViolation("refused")
        answer = model.structured(Summary, [{"role": "user", "content": text}])
        SEEN.append({"topic": answer.topic, "model": model.model})
        return StageResult()

    r.stage(
        "summaries.topic",
        summarize,
        version=1,
        requires=("mime",),
        produces=("topic",),
        lane="llm-local",
        roles=("summary",),
    )


Summaries = make_module("summaries", register=register_summaries)


def build(tmp_path: Path, server: FakeModelServer | None) -> Runtime:
    llm: dict[str, Any] = {}
    if server is not None:
        cfg = llm_config(server.url)
        cfg["roles"]["summary"] = {
            "endpoint": "local",
            "model": "fake-chat",
            "structured": "json_schema",
        }
        llm["llm"] = cfg
    runtime = make_runtime(tmp_path, (Summaries,), **llm)
    root = tmp_path / "lib"
    root.mkdir(exist_ok=True)
    (root / "a.txt").write_text("notes about gardening")
    (root / "b.txt").write_text("PRIVACY test")
    with runtime.dbs.state.write() as conn:
        store.upsert_library(conn, "lib", root=str(root), actor=ACTOR)
    Scanner(runtime.dbs, DiscoverySettings()).scan("lib")
    return runtime


def status_of(runtime: Runtime, name: str) -> Any:
    with runtime.dbs.state.read() as conn:
        content_hash = conn.execute(
            select(files.c.content_hash).where(files.c.relpath == name)
        ).scalar_one()
    with runtime.dbs.index.read() as conn:
        return conn.execute(
            select(stage_runs).where(
                stage_runs.c.content_hash == content_hash,
                stage_runs.c.stage == "summaries.topic",
            )
        ).one_or_none()


def test_a_stage_gets_structured_answers_through_the_gateway(tmp_path: Path) -> None:
    SEEN.clear()
    with model_server() as server:
        server.replies = ['```json\n{"topic": 7}\n```', '{"topic": "gardening"}']
        runtime = build(tmp_path, server)
        try:
            Worker(runtime).drain()
            assert SEEN == [{"topic": "gardening", "model": "fake-chat"}]
            run = status_of(runtime, "a.txt")
            assert (run.status, run.model) == ("done", "summary=fake-chat")
            first, retry = server.chats()
            assert first["response_format"]["type"] == "json_schema"
            assert first["temperature"] == 0
            assert first["model"] == "fake-chat"
            assert "topic: Input should be a valid string" in retry["messages"][-1]["content"]
            skipped = status_of(runtime, "b.txt")
            assert (skipped.status, skipped.error) == (
                "skipped",
                "not sent to the model (privacy rules)",
            )
        finally:
            runtime.close()


def test_without_the_role_the_stage_waits_for_a_mapping(tmp_path: Path) -> None:
    runtime = build(tmp_path, None)
    try:
        assert "summaries.topic" in runtime.stages.inactive
        Worker(runtime).drain()
        assert status_of(runtime, "a.txt") is None
    finally:
        runtime.close()


def test_postpone_keeps_the_attempt(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    try:
        with runtime.dbs.state.write() as conn:
            job_id = queue.enqueue(conn, "x.y", lane="io")
            from datetime import timedelta

            job = queue.claim(
                conn, lane="io", types=["x.y"], worker_id="w", lease=timedelta(minutes=1)
            )
            assert job is not None
            assert job.attempts == 1
            assert queue.postpone(
                conn, job_id, worker_id="w", delay=timedelta(minutes=5), reason="later"
            )
            assert not queue.postpone(
                conn, job_id, worker_id="w", delay=timedelta(minutes=5), reason="again"
            )
            row = conn.execute(select(jobs).where(jobs.c.job_id == job_id)).one()
        assert (row.status, row.attempts, row.last_error) == ("queued", 0, "later")
        assert row.run_after > utcnow()
    finally:
        runtime.close()


# --- CLI -------------------------------------------------------------------------------------


def cli(config: Path, *args: str) -> Any:
    return CliRunner().invoke(app, ["--config-dir", str(config), *args])


def test_models_list_and_probe(tmp_path: Path) -> None:
    with model_server() as server:
        llm = llm_config(server.url)
        config = write_config(
            tmp_path / "config", steward={"data_dir": str(tmp_path / "data")}, llm=llm
        )
        assert cli(config, "db", "upgrade").exit_code == 0
        listed = cli(config, "models", "list")
        assert listed.exit_code == 0, listed.output
        assert "embed [embed] -> local/fake-embed" in listed.output
        assert "stages: search.embed" in listed.output
        assert "used by no enabled module: chat" in listed.output

        server.replies = ["ready", json.dumps({"color": "blue", "count": 3})]
        probed = cli(config, "models", "probe")
        assert probed.exit_code == 0, probed.output
        assert "ok    embed: 32 dimensions" in probed.output
        assert "ok    reply: answered" in probed.output
        assert "ok    structured (prompted): valid JSON (blue, 3)" in probed.output
        assert all("gardening" not in t for t in server.embedded())

        server.status = 500
        failed = cli(config, "models", "probe", "embed")
        assert failed.exit_code == 1
        assert "FAIL  embed: unavailable" in failed.output
        assert cli(config, "models", "probe", "ghost").exit_code == 1


def test_models_probe_reports_a_dims_mismatch(tmp_path: Path) -> None:
    with model_server(dims=8) as server:
        config = write_config(
            tmp_path / "config",
            steward={"data_dir": str(tmp_path / "data")},
            llm=llm_config(server.url),
        )
        assert cli(config, "db", "upgrade").exit_code == 0
        result = cli(config, "models", "probe", "embed")
        assert result.exit_code == 1
        assert "8 dimensions, but llm.yaml says dims: 32" in result.output


def test_stages_list_shows_stages_that_dont_run(tmp_path: Path) -> None:
    config = write_config(tmp_path / "config", steward={"data_dir": str(tmp_path / "data")})
    assert cli(config, "db", "upgrade").exit_code == 0
    result = cli(config, "stages", "list")
    assert result.exit_code == 0, result.output
    assert "search.embed  (not running: role 'embed' is not mapped" in result.output
