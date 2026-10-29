# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Model roles: declarations, matching against llm.yaml, graph filtering and model keys,
structured output, the gateway's circuit breaker, the probe image."""

from __future__ import annotations

import struct
import zlib
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from support import make_runtime

from steward.core.config import EndpointConfig, LlmConfig, RoleConfig
from steward.core.db import utcnow
from steward.core.llm import ChatRequest, Gateway, ModelUnavailable, model_key, structured
from steward.core.llm.probe import red_png
from steward.core.llm.roles import RoleDeclarationError, resolve_roles
from steward.core.llm.transport import ModelRequest, ModelResponse
from steward.core.privacy import PrivacyRules
from steward.core.stages import StageGraphError, build_graph
from steward.core.stages.planner import RunRecord, run_status
from steward.sdk import (
    ModelRole,
    Registrations,
    Registry,
    Stage,
    StageContext,
    StageResult,
    StructuredOutputError,
)


def noop(ctx: StageContext) -> StageResult | None:
    return None


ROOT = Stage("core.hash", 1, ("hash", "mime"), noop, lane="io")


def llm(**roles: dict[str, Any]) -> LlmConfig:
    return LlmConfig(
        config_version=1,
        endpoints={"local": EndpointConfig(base_url="http://model:1/v1", trust="local")},
        roles={name: RoleConfig(endpoint="local", **spec) for name, spec in roles.items()},
    )


DECLARED = {
    "embed": [("search", ModelRole("embed", "embed", "embeddings"))],
    "caption": [("photos", ModelRole("caption", "chat", "captions", vision=True))],
}


def graph_stages() -> dict[str, Stage]:
    return {
        "core.hash": ROOT,
        "docs.chunk": Stage("docs.chunk", 1, ("chunks",), noop, requires=("mime",)),
        "search.embed": Stage(
            "search.embed", 1, ("embeddings",), noop, requires=("chunks",), roles=("embed",)
        ),
        "photos.caption": Stage(
            "photos.caption", 1, ("caption",), noop, requires=("mime",), roles=("caption",)
        ),
        "photos.index": Stage("photos.index", 1, ("caption_idx",), noop, requires=("caption",)),
        "photos.extra": Stage(
            "photos.extra", 1, ("extra",), noop, requires=("mime",), optional=("caption",)
        ),
    }


# --- declarations ----------------------------------------------------------------------


def test_model_role_declarations_are_validated() -> None:
    r = Registry("photos", None, Registrations())
    r.model_role("caption", kind="chat", description="Describes photos", vision=True)
    with pytest.raises(ValueError, match="snake_case"):
        r.model_role("Caption", kind="chat", description="x")
    with pytest.raises(ValueError, match="kind"):
        r.model_role("other", kind="vision", description="x")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="only chat roles"):
        r.model_role("vec", kind="embed", description="x", vision=True)
    with pytest.raises(ValueError, match="description"):
        r.model_role("blank", kind="chat", description="  ")
    with pytest.raises(ValueError, match="bad role name"):
        r.stage("photos.x", noop, version=1, produces=("x",), roles=("Bad-Name",))


def test_several_modules_may_declare_a_role_with_the_same_kind() -> None:
    regs = Registrations()
    Registry("search", None, regs).model_role("embed", kind="embed", description="a")
    Registry("photos", None, regs).model_role("embed", kind="embed", description="b")
    states = resolve_roles(regs.model_roles(), llm(embed={"model": "e", "dims": 8}))
    assert states["embed"].modules == ("search", "photos")
    assert states["embed"].usable
    Registry("tags", None, regs).model_role("embed", kind="chat", description="c")
    with pytest.raises(RoleDeclarationError, match="different kinds"):
        resolve_roles(regs.model_roles(), llm())


def test_roles_need_a_mapping_and_vision_when_asked() -> None:
    states = resolve_roles(DECLARED, llm(caption={"model": "small"}))
    assert states["embed"].problem == "not mapped to a model in llm.yaml"
    assert "vision" in (states["caption"].problem or "")
    states = resolve_roles(
        DECLARED, llm(embed={"model": "e"}, caption={"model": "v", "capabilities": ["vision"]})
    )
    assert all(s.usable for s in states.values())
    assert states["caption"].model_key == "v"
    assert states["embed"].model_key == model_key(
        RoleConfig(endpoint="local", model="e"), embed=True
    )


def test_embedding_model_keys_cover_size_and_prefix() -> None:
    base = RoleConfig(endpoint="local", model="e", dims=8)
    keys = {
        model_key(base, embed=True),
        model_key(base.model_copy(update={"dims": 16}), embed=True),
        model_key(base.model_copy(update={"document_prefix": "passage: "}), embed=True),
        model_key(base.model_copy(update={"model": "f"}), embed=True),
    }
    assert len(keys) == 4
    # the query prefix and threshold don't change stored vectors
    assert model_key(base.model_copy(update={"query_prefix": "q: "}), embed=True) in keys


# --- the graph -----------------------------------------------------------------------------


def test_stages_with_unmapped_roles_and_their_dependents_are_left_out() -> None:
    graph = build_graph(graph_stages(), resolve_roles(DECLARED, llm()))
    assert set(graph.stages) == {"core.hash", "docs.chunk", "photos.extra"}
    assert "role 'embed'" in graph.inactive["search.embed"]
    assert "role 'caption'" in graph.inactive["photos.caption"]
    assert "inactive stage 'photos.caption'" in graph.inactive["photos.index"]
    assert graph.optional_upstream("photos.extra") == ()  # optional input: still runs
    assert graph.model_keys == {}


def test_mapping_or_changing_a_model_changes_the_signature() -> None:
    off = build_graph(graph_stages(), resolve_roles(DECLARED, llm()))
    on = build_graph(graph_stages(), resolve_roles(DECLARED, llm(embed={"model": "e"})))
    other = build_graph(graph_stages(), resolve_roles(DECLARED, llm(embed={"model": "f"})))
    assert "search.embed" in on.stages
    assert len({off.signature, on.signature, other.signature}) == 3
    assert on.model_keys["search.embed"].startswith("embed=e|")
    # stages without roles keep their old signature shape
    plain = {k: v for k, v in graph_stages().items() if not v.roles and k != "photos.index"}
    assert build_graph(plain).signature == build_graph(plain, {}).signature


def test_a_stage_using_an_undeclared_role_stops_startup() -> None:
    stages = {"core.hash": ROOT, "x.y": Stage("x.y", 1, ("y",), noop, roles=("ghost",))}
    with pytest.raises(StageGraphError, match="no enabled module declares"):
        build_graph(stages, {})


def test_results_made_with_another_model_are_not_current() -> None:
    graph = build_graph(graph_stages(), resolve_roles(DECLARED, llm(embed={"model": "e"})))
    now = utcnow()
    key = graph.model_keys["search.embed"]
    runs = {
        "core.hash": RunRecord(1, "done", now),
        "docs.chunk": RunRecord(1, "done", now),
        "search.embed": RunRecord(1, "done", now + timedelta(seconds=1), key),
    }
    assert "search.embed" in run_status(graph, runs)[1]
    runs["search.embed"] = RunRecord(1, "done", now + timedelta(seconds=1), "embed=old")
    assert "search.embed" not in run_status(graph, runs)[0]


# --- structured output ---------------------------------------------------------------------


class Answer(BaseModel):
    color: str
    count: int


def scripted(*answers: str) -> tuple[list[ChatRequest], Any]:
    sent: list[ChatRequest] = []
    queue = list(answers)

    def send(request: ChatRequest) -> str:
        sent.append(request)
        return queue.pop(0)

    return sent, send


@pytest.mark.parametrize("method", ["json_schema", "json_mode", "prompted"])
def test_structured_output_uses_the_roles_method(method: str) -> None:
    role = RoleConfig(endpoint="local", model="m", structured=method)
    sent, send = scripted('{"color": "blue", "count": 3}')
    answer = structured.run(send, role, Answer, [{"role": "user", "content": "q"}])
    assert answer == Answer(color="blue", count=3)
    options = sent[0].options
    system = sent[0].messages[0]["content"]
    if method == "json_schema":
        assert options["response_format"]["type"] == "json_schema"
        assert options["response_format"]["json_schema"]["schema"]["required"] == [
            "color",
            "count",
        ]
        assert "schema" not in system
    else:
        assert options.get("response_format") == (
            {"type": "json_object"} if method == "json_mode" else None
        )
        assert '"count"' in system


@pytest.mark.parametrize(
    "text",
    [
        '```json\n{"color": "red", "count": 1}\n```',
        '<think>hmm, let me see</think>\n{"color": "red", "count": 1}',
        'Sure! Here it is: {"color": "red", "count": 1} Hope that helps.',
    ],
)
def test_answers_are_cleaned_before_validation(text: str) -> None:
    role = RoleConfig(endpoint="local", model="m")
    _, send = scripted(text)
    assert structured.run(send, role, Answer, []).color == "red"


def test_validation_errors_go_back_to_the_model() -> None:
    role = RoleConfig(endpoint="local", model="m")
    sent, send = scripted('{"color": "red"}', '{"color": "red", "count": 2}')
    assert structured.run(send, role, Answer, []).count == 2
    retry = sent[1].messages
    assert retry[-2] == {"role": "assistant", "content": '{"color": "red"}'}
    assert "count: Field required" in retry[-1]["content"]


def test_structured_output_gives_up_without_quoting_the_answer() -> None:
    role = RoleConfig(endpoint="local", model="m")
    _, send = scripted(*['{"color": "SECRET-TEXT", "count": "many"}'] * 3)
    with pytest.raises(StructuredOutputError) as caught:
        structured.run(send, role, Answer, [], retries=2)
    assert "3 tries" in str(caught.value)
    assert "SECRET-TEXT" not in str(caught.value)
    assert "many" not in str(caught.value)


# --- the circuit breaker -------------------------------------------------------------------


class FlakyTransport:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = True

    def send(self, endpoint: EndpointConfig, model: str, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.fail:
            raise ModelUnavailable("endpoint unreachable: ConnectionRefusedError")
        return ModelResponse({"choices": [{"message": {"content": "hi"}}]})


def test_a_failed_endpoint_fails_fast_then_backs_off(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    now = [1000.0]
    transport = FlakyTransport()
    gateway = Gateway(
        llm(chat={"model": "c"}),
        runtime.dbs.state,
        PrivacyRules.default(),
        transport,
        clock=lambda: now[0],
    )
    request = ChatRequest([{"role": "user", "content": "hi"}])

    def call() -> None:
        gateway.call("chat", request, provenance=[], purpose="index")

    with pytest.raises(ModelUnavailable) as caught:
        call()
    assert caught.value.retry_after_s == 30
    with pytest.raises(ModelUnavailable, match="failed recently"):
        call()
    assert transport.calls == 1
    now[0] += 31
    with pytest.raises(ModelUnavailable) as caught:
        call()
    assert caught.value.retry_after_s == 60
    assert transport.calls == 2
    now[0] += 61
    transport.fail = False
    call()
    assert gateway.down_for("local") == 0
    runtime.close()


# --- probe -----------------------------------------------------------------------------------


def test_the_probe_image_is_a_valid_red_png() -> None:
    data = red_png(4)
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (4, 4)
    start = data.index(b"IDAT") + 4
    length = struct.unpack(">I", data[start - 8 : start - 4])[0]
    raw = zlib.decompress(data[start : start + length])
    assert raw[:4] == b"\x00\xff\x00\x00"
