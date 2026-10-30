# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Structured output: a chat answer validated against a Pydantic model (plan §11.2).

The role's `structured` setting picks the method:
- `json_schema`: the request carries the schema as `response_format` (servers that
  support it constrain the output);
- `json_mode`: `response_format: json_object`, schema described in the prompt;
- `prompted`: only the prompt asks for JSON matching the schema.

Answers are cleaned (reasoning blocks and code fences removed), then validated. On a
validation error the error (never the answer) goes back to the model, up to `retries`
times; then `StructuredOutputError`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from steward.core.config import RoleConfig
from steward.core.llm.transport import ChatRequest
from steward.sdk import Message, StructuredOutputError

_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?```$", re.S)
_NAME = re.compile(r"[^a-zA-Z0-9_-]")
MAX_ERRORS = 10
TEMPERATURE = 0
"""Structured answers are read off a document, not written: the same file should give the
same tags and fields. At the model's default, 5 of 69 files changed tags on a re-run."""


def instruction(schema: type[BaseModel], *, with_schema: bool) -> str:
    text = "Answer with a single JSON object and nothing else."
    if with_schema:
        text += " It must match this JSON schema:\n" + json.dumps(
            schema.model_json_schema(), ensure_ascii=False
        )
    return text


def first_request(
    role: RoleConfig, schema: type[BaseModel], messages: Sequence[Message]
) -> ChatRequest:
    options: dict[str, Any] = {"temperature": TEMPERATURE}
    if role.structured == "json_schema":
        options["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": _NAME.sub("_", schema.__name__)[:64] or "answer",
                "schema": schema.model_json_schema(),
            },
        }
    elif role.structured == "json_mode":
        options["response_format"] = {"type": "json_object"}
    system = {
        "role": "system",
        "content": instruction(schema, with_schema=role.structured != "json_schema"),
    }
    return ChatRequest([system, *messages], options)


def clean(text: str) -> str:
    """The JSON part of an answer: without reasoning blocks and code fences."""
    text = _THINK.sub("", text).strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith(("{", "[")):
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            text = text[start : end + 1]
    return text


def describe(exc: ValidationError) -> str:
    """The validation errors without the input values (they may quote file content)."""
    parts = []
    for err in exc.errors(include_input=False, include_url=False)[:MAX_ERRORS]:
        where = ".".join(str(p) for p in err["loc"]) or "(answer)"
        parts.append(f"{where}: {err['msg']}")
    return "; ".join(parts)


def run[M: BaseModel](
    send: Callable[[ChatRequest], str],
    role: RoleConfig,
    schema: type[M],
    messages: Sequence[Message],
    *,
    retries: int = 2,
) -> M:
    """Ask, validate, and re-ask with the error. `send` returns the answer's text."""
    if retries < 0:
        raise ValueError("retries must be >= 0")
    request = first_request(role, schema, messages)
    error = ""
    for _ in range(retries + 1):
        answer = send(request)
        try:
            return schema.model_validate_json(clean(answer))
        except ValidationError as exc:
            error = describe(exc)
        request = ChatRequest(
            [
                *request.messages,
                {"role": "assistant", "content": answer},
                {
                    "role": "user",
                    "content": f"That answer isn't valid ({error}). "
                    + instruction(schema, with_schema=True),
                },
            ],
            request.options,
        )
    raise StructuredOutputError(f"no valid {schema.__name__} after {retries + 1} tries: {error}")
