# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`documents.ocr` end to end with a stand-in `tesseract`: scanned and mixed PDFs, images,
forced OCR, optional inputs (`chunk` uses `text:ocr` when it exists), the folder and
privacy rules, the health check, and back-fill when the program appears later."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from doc_fixtures import make_encrypted_pdf, make_pdf
from ocr_support import SCANNED_WORD, calls, make_fake_tesseract
from sqlalchemy import select
from support import eps, write_config

from steward.core.config import DiscoverySettings, load_config
from steward.core.db.schema import chunks, files, stage_runs, texts
from steward.core.discovery import Scanner
from steward.core.folders import FolderRef, StageChanges, TypeDefinition, load_policy, store
from steward.core.runtime import Runtime, build_runtime
from steward.core.search import search_all
from steward.core.stages import enqueue_full_pass, full_pass_needed
from steward.core.worker import Worker
from steward.modules.documents import DocumentsModule

ACTOR = "user:1"
PNG = b"\x89PNG\r\n\x1a\n"

LAYOUTS: dict[str, dict[str, str]] = {
    "numbered": {
        "docs": "10-documents",
        "scans": "15-scans",
        "media": "20-media",
        "private": "90-private",
    },
    "flat": {"docs": "Documents", "scans": "Scanner", "media": "Photos", "private": "Private"},
}


class Env:
    def __init__(self, tmp_path: Path, layout: str, **settings: Any) -> None:
        self.tmp_path = tmp_path
        self.spec = LAYOUTS[layout]
        self.fake = make_fake_tesseract(tmp_path / "bin")
        self.settings = {"ocr_command": str(self.fake), **settings}
        self.root = tmp_path / "lib"
        d, s = self.spec["docs"], self.spec["scans"]
        self.files: dict[str, bytes] = {
            f"{d}/scan.pdf": make_pdf([""], images={1}),
            f"{d}/mixed.pdf": make_pdf(["Typed introduction page", ""], images={2}),
            f"{d}/typed.pdf": make_pdf(["Typed letter about the harbour"], images={1}),
            f"{d}/blank-page.pdf": make_pdf(["Typed cover sheet", ""]),
            f"{d}/locked-out.pdf": make_encrypted_pdf([""]),
            f"{d}/receipt.png": PNG + b"SAY:receipt espresso total\n",
            f"{d}/notes.md": b"# Notes\nplain markdown stays as it was\n",
            f"{s}/typed-scan.pdf": make_pdf(["Typed but rescanned"]),
            f"{self.spec['media']}/photo.png": PNG + b"SAY:beachword sunset\n",
            f"{self.spec['private']}/secret.png": PNG + b"SAY:lockedword memo\n",
        }
        for relpath, data in self.files.items():
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.runtime = self.build()
        with self.runtime.dbs.state.write() as conn:
            store.upsert_library(conn, "lib", root=str(self.root), actor=ACTOR)
            store.define_type(
                conn,
                "scans",
                TypeDefinition(extends="documents", stages=StageChanges(add=("ocr",))),
                actor=ACTOR,
            )
            for key, type_name in (
                ("docs", "documents"),
                ("scans", "scans"),
                ("media", "media"),
                ("private", "private"),
            ):
                store.set_type(conn, FolderRef("lib", self.spec[key]), type_name, actor=ACTOR)
            store.ensure_default_roles(conn, actor=ACTOR)
        self.scan()

    def build(self) -> Runtime:
        config_dir = write_config(
            self.tmp_path / "config",
            steward={"data_dir": str(self.tmp_path / "data")},
            modules={"modules": {"documents": {"enabled": True, "settings": self.settings}}},
        )
        runtime = build_runtime(load_config(config_dir), entry_points=eps(DocumentsModule))
        runtime.upgrade_databases()
        return runtime

    def rebuild(self, **settings: Any) -> None:
        self.runtime.close()
        self.settings = {**self.settings, **settings}
        self.runtime = self.build()

    def scan(self) -> None:
        Scanner(self.runtime.dbs, DiscoverySettings()).scan("lib")
        Worker(self.runtime).drain()

    def hash_of(self, relpath: str) -> str:
        with self.runtime.dbs.state.read() as conn:
            value = conn.execute(
                select(files.c.content_hash).where(files.c.relpath == relpath)
            ).scalar_one()
        assert value is not None
        return str(value)

    def runs(self, relpath: str) -> dict[str, tuple[str, str | None]]:
        with self.runtime.dbs.index.read() as conn:
            rows = conn.execute(
                select(stage_runs.c.stage, stage_runs.c.status, stage_runs.c.error).where(
                    stage_runs.c.content_hash == self.hash_of(relpath)
                )
            ).all()
        return {r.stage: (r.status, r.error) for r in rows}

    def text(self, relpath: str, artifact: str) -> list[tuple[int | None, str]]:
        with self.runtime.dbs.index.read() as conn:
            rows = conn.execute(
                select(texts.c.page, texts.c.body)
                .where(texts.c.content_hash == self.hash_of(relpath), texts.c.artifact == artifact)
                .order_by(texts.c.part)
            ).all()
        return [(r.page, r.body.strip()) for r in rows]

    def chunk_source(self, relpath: str) -> set[str]:
        with self.runtime.dbs.index.read() as conn:
            return set(
                conn.execute(
                    select(chunks.c.source).where(chunks.c.content_hash == self.hash_of(relpath))
                ).scalars()
            )

    def find(self, query: str, *, scope: str = "normal") -> list[str]:
        with self.runtime.dbs.state.read() as conn:
            policy = load_policy(conn)
        results = search_all(self.runtime.dbs, policy, query, scope=scope, mode="text")  # type: ignore[arg-type]
        return sorted(h.relpath for h in results.hits)

    def health(self) -> tuple[str, str]:
        check = self.runtime.registrations.health_checks()["documents.ocr"]
        result = asyncio.run(check())
        return result.status, result.detail


@pytest.fixture(params=sorted(LAYOUTS))
def env(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path, request.param)
    yield e
    e.runtime.close()


def test_a_scanned_pdf_becomes_searchable(env: Env) -> None:
    d = env.spec["docs"]
    runs = env.runs(f"{d}/scan.pdf")
    assert runs["documents.extract"] == ("done", None)
    assert runs["documents.ocr"] == ("done", None)
    assert runs["documents.chunk"] == ("done", None)
    assert env.text(f"{d}/scan.pdf", "text") == []
    assert env.text(f"{d}/scan.pdf", "text:ocr") == [(1, SCANNED_WORD)]
    assert env.chunk_source(f"{d}/scan.pdf") == {"text:ocr"}
    assert f"{d}/scan.pdf" in env.find(SCANNED_WORD)


def test_only_pages_without_text_are_ocrd(env: Env) -> None:
    d = env.spec["docs"]
    assert env.text(f"{d}/mixed.pdf", "text:ocr") == [
        (1, "Typed introduction page"),
        (2, SCANNED_WORD),
    ]
    assert env.find("introduction") == [f"{d}/mixed.pdf"]
    # Rendered pages go to tesseract as PGM with the render resolution.
    pages = [c for c in calls(env.fake) if c["pgm"]]
    assert pages
    assert all(c["args"][:4] == ["stdin", "stdout", "-l", "eng"] for c in pages)
    assert all(c["args"][4:] == ["--dpi", "300"] for c in pages)
    assert all(c["env"] == ["LC_ALL", "OMP_THREAD_LIMIT", "PATH"] for c in pages)


def test_documents_with_text_are_left_alone(env: Env) -> None:
    d = env.spec["docs"]
    # Text plus an image, and text plus a blank page (no image): nothing to OCR.
    for name in ("typed.pdf", "blank-page.pdf"):
        assert env.runs(f"{d}/{name}")["documents.ocr"] == ("skipped", "has a text layer")
        assert env.chunk_source(f"{d}/{name}") == {"text"}
    assert env.find("harbour") == [f"{d}/typed.pdf"]
    # 2 scanned pages + 1 forced page; images are fed directly.
    assert sum(c["pgm"] for c in calls(env.fake)) == 3
    # Markdown isn't something OCR applies to.
    assert "documents.ocr" not in env.runs(f"{d}/notes.md")


def test_a_type_that_adds_ocr_ocrs_every_page(env: Env) -> None:
    path = f"{env.spec['scans']}/typed-scan.pdf"
    assert env.runs(path)["documents.ocr"] == ("done", None)
    assert env.text(path, "text:ocr") == [(1, SCANNED_WORD)]
    assert env.chunk_source(path) == {"text:ocr"}


def test_images_are_ocrd_except_in_media_folders(env: Env) -> None:
    d = env.spec["docs"]
    assert env.text(f"{d}/receipt.png", "text:ocr") == [(None, "receipt espresso total")]
    assert env.runs(f"{d}/receipt.png")["documents.chunk"] == ("done", None)
    assert "documents.extract" not in env.runs(f"{d}/receipt.png")
    assert env.find("espresso") == [f"{d}/receipt.png"]
    assert env.runs(f"{env.spec['media']}/photo.png") == {"core.hash": ("done", None)}
    assert env.find("beachword") == []


def test_ocr_text_of_locked_files_stays_in_the_locked_folder(env: Env) -> None:
    path = f"{env.spec['private']}/secret.png"
    assert env.runs(path)["documents.ocr"] == ("done", None)
    assert env.find("lockedword") == []
    assert env.find("lockedword", scope="locked") == [path]


def test_an_encrypted_pdf_is_skipped(env: Env) -> None:
    runs = env.runs(f"{env.spec['docs']}/locked-out.pdf")
    assert runs["documents.extract"] == ("skipped", "encrypted")
    assert runs["documents.ocr"] == ("skipped", "encrypted")
    assert runs["documents.chunk"] == ("skipped", "no text")


def test_a_failing_tesseract_keeps_nothing_of_its_output(env: Env) -> None:
    path = env.root / env.spec["docs"] / "bad.png"
    path.write_bytes(PNG + b"FAIL secret-ocr-output\n")
    env.scan()
    rel = f"{env.spec['docs']}/bad.png"
    assert env.runs(rel)["documents.ocr"] == ("skipped", "OCR failed")
    with env.runtime.dbs.index.read() as conn:
        errors = conn.execute(select(stage_runs.c.error)).scalars().all()
    assert not any("secret" in (e or "") for e in errors)


def test_health_check_reports_ok(env: Env) -> None:
    assert env.health() == ("ok", "Tesseract, languages eng")


def test_rerunning_changes_nothing(env: Env) -> None:
    before = len(calls(env.fake))
    with env.runtime.dbs.state.write() as conn:
        enqueue_full_pass(conn)
    Worker(env.runtime).drain()
    assert len(calls(env.fake)) == before


def test_without_tesseract_ocr_is_unavailable_and_backfills_later(tmp_path: Path) -> None:
    e = Env(tmp_path, "flat", ocr_command=str(tmp_path / "missing" / "tesseract"))
    try:
        assert "documents.ocr" not in e.runtime.stages.stages
        status, detail = e.health()
        assert status == "warn"
        assert detail.startswith("OCR unavailable:")
        # Everything else still works; scans just have no text.
        assert e.runs("Documents/scan.pdf")["documents.chunk"] == ("skipped", "no text")
        assert e.find("harbour") == ["Documents/typed.pdf"]
        assert e.runs("Documents/receipt.png") == {"core.hash": ("done", None)}

        e.rebuild(ocr_command=str(e.fake))  # the program was installed
        assert full_pass_needed(e.runtime.dbs, e.runtime.stages)
        with e.runtime.dbs.state.write() as conn:
            enqueue_full_pass(conn)
        Worker(e.runtime).drain()
        assert e.find(SCANNED_WORD) == [
            "Documents/mixed.pdf",
            "Documents/scan.pdf",
            "Scanner/typed-scan.pdf",
        ]
        assert e.find("espresso") == ["Documents/receipt.png"]
        assert e.chunk_source("Documents/typed.pdf") == {"text"}
    finally:
        e.runtime.close()


def test_turned_off(tmp_path: Path) -> None:
    e = Env(tmp_path, "flat", ocr=False)
    try:
        assert "documents.ocr" not in e.runtime.stages.stages
        assert e.health() == ("ok", "OCR is turned off")
        assert calls(e.fake) == []
    finally:
        e.runtime.close()


def test_missing_language_data(tmp_path: Path) -> None:
    e = Env(tmp_path, "flat", ocr_languages=["deu"])
    try:
        assert e.health() == ("warn", "OCR unavailable: no data for deu")
        assert e.runs("Documents/scan.pdf")["documents.ocr"] == (
            "skipped",
            "no OCR language data (deu)",
        )
        assert calls(e.fake) == []  # (language listings aren't logged)
    finally:
        e.runtime.close()


def test_some_languages_missing(tmp_path: Path) -> None:
    e = Env(tmp_path, "flat", ocr_languages=["eng", "deu"])
    try:
        assert e.health() == ("warn", "OCR language data missing: deu")
        assert calls(e.fake)
        assert all(c["args"][3] == "eng" for c in calls(e.fake))
    finally:
        e.runtime.close()


@pytest.mark.parametrize("bad", [[], ["eng;rm"], ["../x"]])
def test_language_names_are_checked(tmp_path: Path, bad: list[str]) -> None:
    from steward.modules.documents import DocumentsConfig

    with pytest.raises(ValueError, match="language"):
        DocumentsConfig(ocr_languages=bad)
