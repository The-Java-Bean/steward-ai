# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The privacy suite (plan §9.6). Required to pass before every release.

Every model call goes through `Gateway`; a fake transport records what would have been
sent. Remote endpoints are refused at startup until the `remote_llm` module exists, so
these tests build gateways with remote endpoints directly to prove the second line of
defence holds too."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from steward_hello import HelloModule
from support import make_runtime, write_config

from steward.core.config import ConfigError, EndpointConfig, LlmConfig, RoleConfig, load_config
from steward.core.db.schema import files, jobs
from steward.core.discovery import enqueue_full_scan
from steward.core.folders import FolderRef, store
from steward.core.llm import (
    ChatRequest,
    Gateway,
    ModelRequest,
    ModelResponse,
    PrivacyViolation,
    RoleNotConfigured,
)
from steward.core.privacy import PrivacyRules, set_override
from steward.core.runtime import Runtime, build_runtime
from steward.core.worker import Worker

ACTOR = "user:1"
SECRET_TEXT = "correct horse battery staple vault entry"
PRIVATE_TEXT = "the private diary entry nobody may read"
NORMAL_TEXT = "an ordinary shopping list"


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, ModelRequest]] = []

    def send(self, endpoint: EndpointConfig, model: str, request: ModelRequest) -> ModelResponse:
        self.sent.append((endpoint.base_url, model, request))
        return ModelResponse({"choices": [{"message": {"content": "ok"}}]})


LLM = LlmConfig(
    config_version=1,
    endpoints={
        "local": EndpointConfig(base_url="http://model-server.invalid/v1", trust="local"),
        "cloud": EndpointConfig(base_url="https://cloud.invalid/v1", trust="remote"),
    },
    roles={
        "chat": RoleConfig(endpoint="cloud", model="big-remote", local_fallback="chat_local"),
        "chat_local": RoleConfig(endpoint="local", model="big-local"),
        "caption": RoleConfig(endpoint="cloud", model="vision-remote"),
        "embed": RoleConfig(endpoint="local", model="embedder"),
    },
)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "lib"
        for relpath, text in {
            "docs/list.txt": NORMAL_TEXT,
            "docs/passwords.kdbx": SECRET_TEXT,
            "docs/.env": SECRET_TEXT,
            "Private/diary.txt": PRIVATE_TEXT,
            "Private/photo.jpg": "\xff\xd8\xff" + PRIVATE_TEXT,
        }.items():
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        pipeline = {"schedules": {"core.reconcile": "off"}}
        self.runtime: Runtime = make_runtime(tmp_path, [HelloModule], pipeline=pipeline)
        with self.runtime.dbs.state.write() as conn:
            store.upsert_library(conn, "lib", root=str(self.root), actor=ACTOR)
            store.set_type(conn, FolderRef("lib", "Private"), "private", actor=ACTOR)
            enqueue_full_scan(conn, "lib")
        Worker(self.runtime).drain()
        self.transport = FakeTransport()
        self.gateway = Gateway(LLM, self.runtime.dbs.state, self.runtime.privacy, self.transport)

    def file_id(self, relpath: str) -> str:
        with self.runtime.dbs.state.read() as conn:
            return str(
                conn.execute(select(files.c.file_id).where(files.c.relpath == relpath)).scalar_one()
            )

    def row(self, relpath: str) -> Any:
        with self.runtime.dbs.state.read() as conn:
            return conn.execute(select(files).where(files.c.relpath == relpath)).one()


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    yield e
    e.runtime.close()


def _chat(text: str = "hi") -> ChatRequest:
    return ChatRequest([{"role": "user", "content": text}])


def test_local_only_content_never_reaches_a_remote_endpoint(env: Env) -> None:
    private = env.file_id("Private/diary.txt")
    with pytest.raises(PrivacyViolation, match="local-only"):
        env.gateway.call("chat", _chat(), provenance=[private], purpose="index")
    assert env.transport.sent == []
    # Normal content may go to the remote role.
    result = env.gateway.call(
        "chat", _chat(), provenance=[env.file_id("docs/list.txt")], purpose="chat"
    )
    assert result.route.endpoint_name == "cloud"
    assert result.route.taint == "normal"


def test_a_tainted_conversation_is_forced_local(env: Env) -> None:
    normal = env.file_id("docs/list.txt")
    with pytest.raises(PrivacyViolation):
        env.gateway.call("chat", _chat(), provenance=[normal], purpose="chat", taint="local-only")
    result = env.gateway.call(
        "chat",
        _chat(),
        provenance=[normal],
        purpose="chat",
        taint="local-only",
        allow_local_fallback=True,
    )
    assert (result.route.role, result.route.switched_from) == ("chat_local", "chat")
    assert result.route.reason
    assert result.route.taint == "local-only"
    assert env.transport.sent[-1][1] == "big-local"


def test_a_remote_caption_role_is_refused_for_private_photos(env: Env) -> None:
    photo = env.file_id("Private/photo.jpg")
    with pytest.raises(PrivacyViolation):
        env.gateway.call("caption", _chat(), provenance=[photo], purpose="index")
    assert env.transport.sent == []


def test_excluded_files_are_never_read_or_processed(env: Env) -> None:
    for relpath in ("docs/passwords.kdbx", "docs/.env"):
        row = env.row(relpath)
        assert row.present
        assert row.content_hash is None
        assert row.hash_kind is None
    with env.runtime.dbs.state.read() as conn:
        stage_jobs = conn.execute(
            select(jobs.c.payload).where(jobs.c.type == "core.stage.hello.lines")
        ).all()
    hashes = {j.payload["content_hash"] for j in stage_jobs}
    # Only readable text files: the private diary is processed too (local indexing is
    # allowed), the secret files never are.
    assert env.row("docs/passwords.kdbx").content_hash not in hashes
    readable = {env.row("docs/list.txt").content_hash, env.row("Private/diary.txt").content_hash}
    assert hashes == readable
    with pytest.raises(PrivacyViolation, match="excluded"):
        env.gateway.call("embed", _chat(), provenance=[env.file_id("docs/.env")], purpose="index")
    assert env.transport.sent == []


def test_a_per_file_override_excludes_a_file_on_the_next_scan(env: Env) -> None:
    file_id = env.file_id("docs/list.txt")
    with env.runtime.dbs.state.write() as conn:
        set_override(conn, file_id, "excluded", actor=ACTOR)
        enqueue_full_scan(conn, "lib")
    Worker(env.runtime).drain()
    assert env.row("docs/list.txt").content_hash is None
    with pytest.raises(PrivacyViolation, match="excluded"):
        env.gateway.call("embed", _chat(), provenance=[file_id], purpose="index")


def test_unknown_files_count_as_local_only_and_locked(env: Env) -> None:
    with pytest.raises(PrivacyViolation, match="local-only"):
        env.gateway.call("chat", _chat(), provenance=["no-such-file"], purpose="index")
    with pytest.raises(PrivacyViolation, match="locked"):
        env.gateway.call("chat_local", _chat(), provenance=["no-such-file"], purpose="chat")
    env.gateway.call("embed", _chat(), provenance=["no-such-file"], purpose="index")


def test_locked_files_never_reach_a_chat_surface(env: Env) -> None:
    diary = env.file_id("Private/diary.txt")
    with pytest.raises(PrivacyViolation, match="locked"):
        env.gateway.call("chat_local", _chat(), provenance=[diary], purpose="chat")
    # Indexing on a local endpoint is allowed, so the Locked Folder view can search it.
    env.gateway.call("embed", _chat(), provenance=[diary], purpose="index")


def test_provenance_is_required_and_checked(env: Env) -> None:
    with pytest.raises(TypeError):
        env.gateway.call("embed", _chat())  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="collection of file ids"):
        env.gateway.call("embed", _chat(), provenance="abc", purpose="index")
    with pytest.raises(RoleNotConfigured):
        env.gateway.call("triage", _chat(), provenance=[], purpose="index")


def test_logs_never_contain_file_content(
    env: Env, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    caplog.set_level(logging.DEBUG)
    with env.runtime.dbs.state.write() as conn:
        enqueue_full_scan(conn, "lib")
    (env.root / "docs/list.txt").write_text(NORMAL_TEXT + " changed")
    Worker(env.runtime).drain()
    env.gateway.call(
        "chat", _chat(NORMAL_TEXT), provenance=[env.file_id("docs/list.txt")], purpose="chat"
    )
    with pytest.raises(PrivacyViolation):
        env.gateway.call(
            "chat",
            _chat(PRIVATE_TEXT),
            provenance=[env.file_id("Private/diary.txt")],
            purpose="chat",
        )
    text = caplog.text + "".join(str(r.__dict__) for r in caplog.records)
    for fixture in (SECRET_TEXT, PRIVATE_TEXT, NORMAL_TEXT):
        assert fixture not in text


def test_endpoints_must_declare_trust_and_remote_ones_are_refused(tmp_path: Path) -> None:
    config_dir = write_config(
        tmp_path / "a",
        steward={"data_dir": str(tmp_path / "data")},
        llm={"endpoints": {"local": {"base_url": "http://m.invalid/v1"}}},
    )
    with pytest.raises(ConfigError, match="trust"):
        load_config(config_dir)
    config_dir = write_config(
        tmp_path / "b",
        steward={"data_dir": str(tmp_path / "data")},
        llm={"endpoints": {"cloud": {"base_url": "https://c.invalid/v1", "trust": "remote"}}},
    )
    with pytest.raises(ConfigError, match="remote_llm"):
        build_runtime(load_config(config_dir), entry_points=[])
    with pytest.raises(ConfigError, match="isn't local"):
        load_config(
            write_config(
                tmp_path / "c",
                llm={
                    "endpoints": {
                        "l": {"base_url": "http://l.invalid/v1", "trust": "local"},
                        "r": {"base_url": "https://r.invalid/v1", "trust": "remote"},
                    },
                    "roles": {
                        "chat": {"endpoint": "l", "model": "m", "local_fallback": "far"},
                        "far": {"endpoint": "r", "model": "m"},
                    },
                },
            )
        )


def test_only_core_llm_talks_to_models() -> None:
    """Nothing outside `steward.core.llm` opens HTTP connections to model servers."""
    src = Path(__file__).resolve().parents[2] / "src" / "steward"
    offenders = [
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        if ("urllib.request" in path.read_text() or "import openai" in path.read_text())
        and "core/llm/" not in str(path)
    ]
    assert offenders == []


def test_privacy_rules_default() -> None:
    assert PrivacyRules.default().level_for(".ssh/id_ed25519") == "excluded"
