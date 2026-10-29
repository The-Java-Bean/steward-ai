# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`steward models probe`: try each mapped role's model and report what works (plan
§11.2). Probes send only fixed test text (and a generated image), never file content."""

from __future__ import annotations

import base64
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from steward.core.config import RoleConfig
from steward.core.llm import structured
from steward.core.llm.gateway import Gateway
from steward.core.llm.transport import ChatRequest, EmbedRequest, ModelRequest, ModelResponse
from steward.sdk import ModelUnavailable, RoleKind, StructuredOutputError


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str


class _Probe(BaseModel):
    color: str
    count: int


def red_png(size: int = 32) -> bytes:
    """A plain red square (PNG), for the vision check."""
    row = b"\x00" + b"\xff\x00\x00" * size
    raw = zlib.compress(row * size)

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


def probe_role(gateway: Gateway, role: str, kind: RoleKind) -> list[Check]:
    mapping = gateway.role_config(role)

    def send(request: ModelRequest) -> ModelResponse:
        return gateway.call(role, request, provenance=[], purpose="index").response

    if kind == "embed":
        return [_check("embed", lambda: _embed(send, mapping))]
    checks = [_check("reply", lambda: _reply(send))]
    checks.append(_check(f"structured ({mapping.structured})", lambda: _structured(send, mapping)))
    if mapping.tools == "native":
        checks.append(_check("tool call", lambda: _tool(send)))
    if "vision" in mapping.capabilities:
        checks.append(_check("vision", lambda: _vision(send)))
    return checks


def _check(name: str, fn: Callable[[], str]) -> Check:
    try:
        return Check(name, True, fn())
    except ModelUnavailable as exc:
        return Check(name, False, f"unavailable: {exc}")
    except (StructuredOutputError, ValueError, KeyError, TypeError, IndexError) as exc:
        return Check(name, False, f"{type(exc).__name__}: {exc}")


def _embed(send: Callable[[ModelRequest], ModelResponse], mapping: RoleConfig) -> str:
    vectors = send(EmbedRequest([mapping.document_prefix + "A test sentence."])).vectors
    if len(vectors) != 1 or not vectors[0]:
        raise ValueError("no vector in the answer")
    dims = len(vectors[0])
    if mapping.dims is not None and mapping.dims != dims:
        raise ValueError(f"{dims} dimensions, but llm.yaml says dims: {mapping.dims}")
    return f"{dims} dimensions"


def _reply(send: Callable[[ModelRequest], ModelResponse]) -> str:
    text = send(
        ChatRequest([{"role": "user", "content": "Reply with the single word: ready"}])
    ).text
    if not text.strip():
        raise ValueError("empty answer")
    return "answered"


def _structured(send: Callable[[ModelRequest], ModelResponse], mapping: RoleConfig) -> str:
    answer = structured.run(
        lambda request: send(request).text,
        mapping,
        _Probe,
        [
            {
                "role": "user",
                "content": "The sky is blue and there are 3 birds. Give the color "
                "of the sky and the number of birds.",
            }
        ],
    )
    return f"valid JSON ({answer.color}, {answer.count})"


def _tool(send: Callable[[ModelRequest], ModelResponse]) -> str:
    tools: list[dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Current weather for a city",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ]
    response = send(
        ChatRequest(
            [{"role": "user", "content": "What's the weather in Paris? Use the tool."}],
            {"tools": tools},
        )
    )
    calls = response.data["choices"][0]["message"].get("tool_calls") or []
    if not calls or calls[0]["function"]["name"] != "get_weather":
        raise ValueError("the model didn't call the tool")
    return "called the tool"


def _vision(send: Callable[[ModelRequest], ModelResponse]) -> str:
    url = "data:image/png;base64," + base64.b64encode(red_png()).decode()
    text = send(
        ChatRequest(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What color is this image? One word."},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                }
            ]
        )
    ).text
    if "red" not in text.lower():
        raise ValueError("the answer didn't name the color")
    return "saw the image"
