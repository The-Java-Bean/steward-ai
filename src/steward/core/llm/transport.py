# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Talking to model endpoints. Only `steward.core.llm` sends anything to a model
(docs/adr/0002, 0004); everything else goes through `Gateway.call`.

The built-in transport speaks the OpenAI-compatible HTTP API with the standard library
(non-streaming chat completions and embeddings). Streaming comes with the chat feature;
structured output is built on chat requests (`structured.py`).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from steward.core.config import EndpointConfig
from steward.sdk import ModelUnavailable

__all__ = [
    "ChatRequest",
    "EmbedRequest",
    "ModelRequest",
    "ModelResponse",
    "ModelTransport",
    "ModelUnavailable",
    "OpenAICompatibleTransport",
]


@dataclass(frozen=True)
class ChatRequest:
    messages: Sequence[Mapping[str, Any]]
    options: Mapping[str, Any] = field(default_factory=dict)
    """Extra request fields (`temperature`, `response_format`, `tools`...)."""
    timeout_s: float | None = None
    """Overrides the endpoint's timeout (interactive requests use a short one)."""


@dataclass(frozen=True)
class EmbedRequest:
    inputs: Sequence[str]
    timeout_s: float | None = None


type ModelRequest = ChatRequest | EmbedRequest


@dataclass(frozen=True)
class ModelResponse:
    data: Mapping[str, Any]
    """The endpoint's JSON answer, as is."""

    @property
    def text(self) -> str:
        """The first choice's message content (chat)."""
        try:
            return str(self.data["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            return ""

    @property
    def vectors(self) -> list[list[float]]:
        """Embeddings in input order (embed)."""
        items = sorted(self.data.get("data", []), key=lambda d: d.get("index", 0))
        return [list(map(float, d["embedding"])) for d in items]


class ModelTransport(Protocol):
    def send(self, endpoint: EndpointConfig, model: str, request: ModelRequest) -> ModelResponse:
        """Send one request; raise `ModelUnavailable` on any network or HTTP error."""
        ...


class OpenAICompatibleTransport:
    def send(self, endpoint: EndpointConfig, model: str, request: ModelRequest) -> ModelResponse:
        if isinstance(request, ChatRequest):
            path = "/chat/completions"
            body: dict[str, Any] = {
                **request.options,
                "model": model,
                "messages": list(request.messages),
            }
        else:
            path = "/embeddings"
            body = {"model": model, "input": list(request.inputs)}
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if endpoint.api_key_env:
            key = os.environ.get(endpoint.api_key_env)
            if not key:
                raise ModelUnavailable(f"environment variable {endpoint.api_key_env} isn't set")
            headers["Authorization"] = f"Bearer {key}"
        http_request = urllib.request.Request(  # noqa: S310 -- base_url is validated http(s)
            endpoint.base_url.rstrip("/") + path,
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        try:
            timeout = request.timeout_s or endpoint.timeout_s
            with urllib.request.urlopen(http_request, timeout=timeout) as response:  # noqa: S310
                data = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise ModelUnavailable(f"endpoint answered HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelUnavailable(f"endpoint unreachable: {type(exc).__name__}") from None
        except ValueError:
            raise ModelUnavailable("endpoint sent something that isn't JSON") from None
        if not isinstance(data, dict):
            raise ModelUnavailable("endpoint sent an unexpected answer")
        return ModelResponse(data)
