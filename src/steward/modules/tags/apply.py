# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The prompt and answer schema for choosing tags.

Prompts live in `prompts/tags/apply.v<N>.md`; `PROMPT_VERSION` picks the one in use.
Raising it changes the stage version, so every file is tagged again.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from functools import cache
from importlib import resources
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from steward.sdk import PropertyValue, TagInfo

PROMPT_VERSION = 1
PROMPT_NAME = f"apply.v{PROMPT_VERSION}"


@cache
def prompt() -> str:
    path = resources.files(__package__).joinpath("prompts", "tags", f"{PROMPT_NAME}.md")
    return path.read_text(encoding="utf-8").strip()


def schema(names: tuple[str, ...], max_tags: int) -> type[BaseModel]:
    """`{"tags": [...]}` with the names as an enum."""
    choice: Any = Literal.__getitem__(names)
    return create_model(
        "TagChoice",
        __config__=ConfigDict(extra="ignore"),
        tags=(list[choice], Field(default_factory=list, max_length=max_tags)),
    )


def vocabulary_text(tags: Sequence[TagInfo]) -> str:
    return "\n".join(f"- {t.name}" + (f": {t.description}" if t.description else "") for t in tags)


def messages(
    tags: Sequence[TagInfo],
    *,
    file_name: str,
    folder_type: str,
    text: str,
    description: str,
    fields: Mapping[str, PropertyValue],
) -> list[dict[str, Any]]:
    about = [f"File name: {file_name}", f"Folder type: {folder_type}"]
    if fields:
        about.append("Facts: " + json.dumps(dict(fields), ensure_ascii=False))
    if description:
        about.append(f"Image description: {description}")
    if text:
        about.append(f"Text (start):\n<<<\n{text}\n>>>")
    return [
        {"role": "system", "content": prompt() + "\n\nThe tags:\n" + vocabulary_text(tags)},
        {"role": "user", "content": "\n\n".join(about)},
    ]
