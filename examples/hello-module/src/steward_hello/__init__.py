# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""A minimal steward-ai module. Copy this package to start a new one.

It shows each kind of registration a module makes: a health check, a pipeline stage
that stores its results in the module's own `index.db` table, an event subscriber, and a
web UI page (mounted behind login by the core).
"""

from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import ClassVar

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from sqlalchemy import Column, Integer, MetaData, Table, Text
from sqlalchemy.dialects.sqlite import insert

from steward.sdk import (
    BlobInfo,
    Event,
    FileInfo,
    FolderInfo,
    HealthResult,
    ModuleConfig,
    ModuleKind,
    Registry,
    StageContext,
    StageResult,
)

log = logging.getLogger(__name__)

# Mirrors migrations/index/0001_line_counts.py. Module tables are named `hello_*`.
metadata = MetaData()
line_counts = Table(
    "hello_line_counts",
    metadata,
    Column("content_hash", Text, primary_key=True),
    Column("lines", Integer, nullable=False),
)

MAX_BYTES = 10 * 1024 * 1024


class HelloConfig(ModuleConfig):
    greeting: str = "hello"


def is_small_text(blob: BlobInfo, file: FileInfo, folder: FolderInfo) -> bool:
    return blob.kind == "text" and blob.size <= MAX_BYTES


def count_lines(ctx: StageContext) -> StageResult | None:
    with ctx.open() as f:
        data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return StageResult("skipped", reason="too large")
    lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    with ctx.index_write() as conn:
        # Upsert: a stage can run more than once for the same content.
        stmt = insert(line_counts).values(content_hash=ctx.blob.content_hash, lines=lines)
        conn.execute(
            stmt.on_conflict_do_update(index_elements=["content_hash"], set_={"lines": lines})
        )
    return None


class HelloModule:
    name: ClassVar[str] = "hello"
    version: ClassVar[str] = "0.3.0"
    kind: ClassVar[ModuleKind] = "feature"
    requires: ClassVar[tuple[str, ...]] = ()
    config_model: ClassVar[type[ModuleConfig] | None] = HelloConfig

    def register(self, r: Registry) -> None:
        greeting = r.config_as(HelloConfig).greeting

        async def check() -> HealthResult:
            return HealthResult("ok", greeting)

        def on_discovered(event: Event) -> None:
            log.info(greeting, extra={"fields": {"file_id": event.payload.get("file_id")}})

        r.health_check("hello", check)
        r.stage(
            "hello.lines",
            count_lines,
            version=1,
            requires=("mime",),
            produces=("hello_lines",),
            lane="cpu",
            applies_to=is_small_text,
        )
        r.subscribe("file.discovered", on_discovered, name="hello.greet")
        r.migrations(Path(__file__).parent / "migrations" / "index", database="index")

        page = APIRouter()

        @page.get("")
        def hello_page() -> HTMLResponse:
            text = html.escape(greeting)
            return HTMLResponse(f"<!doctype html><title>Hello</title><h1>{text}</h1>")

        r.ui_page("hello.page", page, path="/ui/hello", title="Hello")
