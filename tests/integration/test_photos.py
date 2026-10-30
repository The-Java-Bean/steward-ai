# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The `photos` module end to end with a fake exiftool and a fake model server, on two
layouts: EXIF properties (also in metadata-only folders; the stage only exists when
exiftool does), captions from the vision role (chunked by `documents`, found by words
and by meaning, kept inside the Locked Folder), degradation without a caption model and
back-fill when one is mapped, and the file page's details."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from api_support import log_in, make_owner
from fastapi.testclient import TestClient
from model_server import FakeModelServer, llm_config, model_server
from ocr_support import make_fake_tesseract
from photos_support import exiftool_calls, make_fake_exiftool, make_jpeg, make_png
from sqlalchemy import select
from support import eps, use_fast_hashes, write_config
from typer.testing import CliRunner

from steward.api import create_app
from steward.core import content, stages
from steward.core.config import DiscoverySettings, load_config
from steward.core.db.schema import chunks, files, stage_runs
from steward.core.discovery import Scanner
from steward.core.folders import FolderRef, load_policy, store
from steward.core.runtime import Runtime, build_runtime
from steward.core.search import SearchResults, search_all
from steward.core.worker import Worker
from steward.modules.documents import DocumentsModule
from steward.modules.files_ui import FilesUiModule
from steward.modules.photos import PhotosModule
from steward.modules.search import SearchModule
from steward.modules.shares import SharesModule

ACTOR = "user:1"
EXIF_STAGE = "photos.exif"
CAPTION_STAGE = "photos.caption"

LAYOUTS: dict[str, dict[str, str]] = {
    "numbered": {"media": "20-media", "archive": "80-archive", "private": "90-private"},
    "flat": {"media": "Photos", "archive": "Old", "private": "Private"},
}
TYPES = {"media": "media", "archive": "archive", "private": "private"}

BEACH_EXIF = {
    "EXIF:DateTimeOriginal": "2023:05:01 12:00:00",
    "EXIF:OffsetTimeOriginal": "+02:00",
    "EXIF:Make": "Canon",
    "EXIF:Model": "Canon EOS R5",
    "Composite:GPSLatitude": 47.5,
    "Composite:GPSLongitude": 8.25,
}
CAPTIONS = {
    (10, 120, 200): "A dog running along a sandy beach under a blue sky.",
    (200, 60, 20): "A red barn in a snowy field.",
    (90, 90, 90): "A plumber fixing a kitchen sink.",
}


def describe(body: dict[str, Any]) -> str:
    """The fake vision model: a description chosen by the image's colour."""
    from base64 import b64decode
    from io import BytesIO

    from PIL import Image

    url = body["messages"][1]["content"][1]["image_url"]["url"]
    with Image.open(BytesIO(b64decode(url.split(",", 1)[1]))) as image:
        pixel = image.convert("RGB").getpixel((5, 5))
    assert isinstance(pixel, tuple)
    for color, text in CAPTIONS.items():
        if all(abs(a - b) < 12 for a, b in zip(pixel, color, strict=True)):
            return text
    return "Something else."


class Env:
    def __init__(
        self,
        tmp_path: Path,
        layout: str,
        server: FakeModelServer | None,
        *,
        exiftool: bool = True,
        caption: bool = True,
        ocr: Path | None = None,
        **settings: Any,
    ) -> None:
        self.tmp_path = tmp_path
        self.settings = settings
        self.ocr = ocr
        self.spec = LAYOUTS[layout]
        self.root = tmp_path / "lib"
        self.fake = make_fake_exiftool(tmp_path / "bin")
        m, a, p = self.spec["media"], self.spec["archive"], self.spec["private"]
        self.paths = {
            "beach": f"{m}/beach.jpg",
            "barn": f"{m}/barn.jpg",
            "icon": f"{m}/icon.png",
            "clip": f"{m}/clip.mp4",
            "old": f"{a}/old.jpg",
            "secret": f"{p}/plumber.jpg",
        }
        contents = {
            "beach": make_jpeg(color=(10, 120, 200), exif=BEACH_EXIF),
            "barn": make_jpeg(color=(200, 60, 20)),
            "icon": make_png((48, 48)),
            "clip": b"\x00\x00\x00\x18ftypmp42 fake video EXIF:"
            + json.dumps({"QuickTime:Duration": 4.5}).encode()
            + b"\n",
            "old": make_jpeg(color=(10, 120, 200), exif={"EXIF:Model": "Old Camera"}),
            "secret": make_jpeg(color=(90, 90, 90)),
        }
        for key, relpath in self.paths.items():
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents[key])
        self.runtime = self.build(server, exiftool=exiftool, caption=caption)
        with self.runtime.dbs.state.write() as conn:
            store.upsert_library(conn, "lib", root=str(self.root), actor=ACTOR)
            for key, type_name in TYPES.items():
                store.set_type(conn, FolderRef("lib", self.spec[key]), type_name, actor=ACTOR)
            store.ensure_default_roles(conn, actor=ACTOR)
        Scanner(self.runtime.dbs, DiscoverySettings()).scan("lib")
        self.drain()

    def build(self, server: FakeModelServer | None, *, exiftool: bool, caption: bool) -> Runtime:
        files: dict[str, Any] = {}
        if server is not None:
            llm = llm_config(server.url)
            if caption:
                llm["roles"]["caption"] = {
                    "endpoint": "local",
                    "model": "fake-vision",
                    "capabilities": ["vision"],
                }
            files["llm"] = llm
        command = str(self.fake) if exiftool else str(self.tmp_path / "missing" / "exiftool")
        settings = {"exif_command": command, **self.settings}
        modules = (DocumentsModule, SearchModule, PhotosModule, FilesUiModule, SharesModule)
        documents = {"ocr_command": str(self.ocr)} if self.ocr else {}
        config_dir = write_config(
            self.tmp_path / "config",
            steward={"data_dir": str(self.tmp_path / "data")},
            modules={
                "modules": {
                    **{mod.name: {"enabled": True} for mod in modules},
                    "documents": {"enabled": True, "settings": documents},
                    "photos": {"enabled": True, "settings": settings},
                }
            },
            **files,
        )
        runtime = build_runtime(load_config(config_dir), entry_points=eps(*modules))
        runtime.upgrade_databases()
        return runtime

    def restart(self, server: FakeModelServer | None, **kwargs: Any) -> None:
        self.runtime.close()
        self.runtime = self.build(server, exiftool=True, caption=True, **kwargs)
        self.drain()

    def drain(self) -> None:
        if stages.full_pass_needed(self.runtime.dbs, self.runtime.stages):
            with self.runtime.dbs.state.write() as conn:
                stages.enqueue_full_pass(conn)
        Worker(self.runtime).drain()

    def hash_of(self, key: str) -> str:
        with self.runtime.dbs.state.read() as conn:
            return str(
                conn.execute(
                    select(files.c.content_hash).where(files.c.relpath == self.paths[key])
                ).scalar_one()
            )

    def run_of(self, key: str, stage: str) -> Any:
        with self.runtime.dbs.index.read() as conn:
            return conn.execute(
                select(stage_runs).where(
                    stage_runs.c.content_hash == self.hash_of(key), stage_runs.c.stage == stage
                )
            ).one_or_none()

    def props(self, key: str) -> dict[str, Any]:
        with self.runtime.dbs.index.read() as conn:
            return content.read_properties(conn, self.hash_of(key), "exif")

    def caption(self, key: str) -> str:
        with self.runtime.dbs.index.read() as conn:
            return " ".join(
                p.body for p in content.read_text(conn, self.hash_of(key), "text:caption")
            )

    def search(self, query: str, *, scope: str = "normal") -> SearchResults:
        with self.runtime.dbs.state.read() as conn:
            policy = load_policy(conn)
        return search_all(
            self.runtime.dbs,
            policy,
            query,
            scope=scope,  # type: ignore[arg-type]
            embed=self.runtime.query_embedder,
        )

    def names(self, query: str, **kwargs: Any) -> list[str]:
        return [h.name for h in self.search(query, **kwargs).hits]

    def health(self, name: str) -> tuple[str, str]:
        result = asyncio.run(self.runtime.registrations.health_checks()[name]())
        return result.status, result.detail


@pytest.fixture
def server() -> Iterator[FakeModelServer]:
    with model_server() as s:
        s.replies = [describe] * 50
        yield s


@pytest.fixture(params=sorted(LAYOUTS))
def layout(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def env(tmp_path: Path, layout: str, server: FakeModelServer) -> Iterator[Env]:
    e = Env(tmp_path, layout, server)
    yield e
    e.runtime.close()


# --- EXIF ----------------------------------------------------------------------------


def test_exif_properties_for_photos_and_videos(env: Env) -> None:
    assert env.props("beach") == {
        "camera": "Canon EOS R5",
        "latitude": 47.5,
        "longitude": 8.25,
        "taken": "2023-05-01T12:00:00+02:00",
    }
    assert env.props("clip") == {"duration_s": 4.5}
    assert env.run_of("barn", EXIF_STAGE).status == "skipped"  # no metadata
    assert env.run_of("barn", EXIF_STAGE).error == "no metadata"
    calls = exiftool_calls(env.fake)
    assert calls
    assert all(c["args"][-1] == "/dev/stdin" for c in calls)  # the file is its stdin
    assert all(set(c["env"]) <= {"LC_ALL", "PATH", "LC_CTYPE"} for c in calls)
    assert env.health(EXIF_STAGE)[0] == "ok"


def test_exif_runs_in_metadata_only_folders_but_captions_dont(env: Env) -> None:
    assert env.props("old") == {"camera": "Old Camera"}
    assert env.run_of("old", CAPTION_STAGE) is None


def test_without_exiftool_the_stage_is_missing_and_health_warns(
    tmp_path: Path, server: FakeModelServer
) -> None:
    env = Env(tmp_path, "flat", server, exiftool=False)
    try:
        assert EXIF_STAGE not in env.runtime.stages.stages
        assert env.props("beach") == {}
        status, detail = env.health(EXIF_STAGE)
        assert status == "warn"
        assert "isn't installed" in detail
        assert env.caption("beach")  # captions don't need exiftool
    finally:
        env.runtime.close()


def test_a_hanging_exiftool_is_stopped(tmp_path: Path, server: FakeModelServer) -> None:
    env = Env(tmp_path, "flat", server, exif_timeout_s=5)
    env.paths["slow"] = "Photos/slow.jpg"
    (env.root / env.paths["slow"]).write_bytes(make_jpeg(size=(100, 100)) + b"SLEEP")
    try:
        Scanner(env.runtime.dbs, DiscoverySettings()).scan("lib")
        env.drain()
        run = env.run_of("slow", EXIF_STAGE)
        assert run.status == "skipped"
        assert run.error == "timed out"
    finally:
        env.runtime.close()


# --- captions ------------------------------------------------------------------------


def test_photos_are_described_and_found_by_words_and_meaning(
    env: Env, server: FakeModelServer
) -> None:
    assert env.caption("beach") == CAPTIONS[(10, 120, 200)]
    run = env.run_of("beach", CAPTION_STAGE)
    assert run.status == "done"
    assert run.prompt_version == "caption.v1"
    assert run.model == "caption=fake-vision"
    chat = server.chats()[0]
    assert chat["model"] == "fake-vision"
    assert chat["temperature"] == 0
    with env.runtime.dbs.index.read() as conn:
        rows = conn.execute(
            select(chunks.c.body, chunks.c.source).where(
                chunks.c.content_hash == env.hash_of("beach")
            )
        ).all()
    assert [(r.body, r.source) for r in rows] == [(CAPTIONS[(10, 120, 200)], "text:caption")]
    found = env.search("sandy beach")
    assert [h.name for h in found.hits] == ["beach.jpg"]
    assert "beach" in "".join(t for t, _ in found.hits[0].snippet)
    meaning = env.search("faucet repair", scope="locked")  # "plumber" ~ "faucet"
    assert [h.name for h in meaning.hits] == ["plumber.jpg"]
    assert meaning.hits[0].semantic


def test_locked_photos_stay_out_of_normal_search(env: Env) -> None:
    assert env.caption("secret") == CAPTIONS[(90, 90, 90)]
    assert "plumber.jpg" not in env.names("plumber kitchen sink")
    assert env.names("plumber kitchen sink", scope="locked") == ["plumber.jpg"]


def test_small_images_are_skipped_without_a_model_call(env: Env, server: FakeModelServer) -> None:
    run = env.run_of("icon", CAPTION_STAGE)
    assert run.status == "skipped"
    assert run.error == "too small"
    assert len(server.chats()) == 3  # beach, barn, the locked one; not the icon


def test_videos_and_non_images_get_no_caption(env: Env) -> None:
    assert env.run_of("clip", CAPTION_STAGE) is None


def test_reasoning_is_removed_from_the_answer(tmp_path: Path, server: FakeModelServer) -> None:
    server.replies = ['<think>Is it a dog?</think>\nCaption: "A dog."'] * 10
    env = Env(tmp_path, "flat", server)
    try:
        assert env.caption("barn") == "A dog."
    finally:
        env.runtime.close()


def test_without_a_caption_model_nothing_is_sent_then_mapping_one_back_fills(
    tmp_path: Path, server: FakeModelServer
) -> None:
    env = Env(tmp_path, "numbered", server, caption=False)
    try:
        assert CAPTION_STAGE in env.runtime.stages.inactive
        assert server.chats() == []
        assert env.names("sandy beach") == []
        status, detail = env.health("core.models")
        assert status == "warn"
        assert "'caption'" in detail
        env.restart(server)
        assert env.caption("beach") == CAPTIONS[(10, 120, 200)]
        assert env.names("sandy beach") == ["beach.jpg"]
    finally:
        env.runtime.close()


def test_a_model_server_outage_makes_captions_wait(tmp_path: Path, server: FakeModelServer) -> None:
    server.status = 500
    env = Env(tmp_path, "flat", server)
    try:
        assert env.run_of("beach", CAPTION_STAGE) is None
        assert env.caption("beach") == ""
    finally:
        env.runtime.close()


def test_an_image_with_text_keeps_its_ocr_text_and_its_description(
    tmp_path: Path, server: FakeModelServer
) -> None:
    fake = make_fake_tesseract(tmp_path / "tess")
    env = Env(tmp_path, "flat", server, ocr=fake)
    try:
        env.paths["receipt"] = "Papers/receipt.jpg"
        receipt = env.root / env.paths["receipt"]
        receipt.parent.mkdir()
        receipt.write_bytes(make_jpeg(color=(200, 60, 20)) + b"SAY:espresso total 4 euro\n")
        Scanner(env.runtime.dbs, DiscoverySettings()).scan("lib")
        env.drain()
        with env.runtime.dbs.index.read() as conn:
            rows = conn.execute(
                select(chunks.c.body, chunks.c.source)
                .where(chunks.c.content_hash == env.hash_of("receipt"))
                .order_by(chunks.c.seq)
            ).all()
        assert [r.body for r in rows] == ["espresso total 4 euro", CAPTIONS[(200, 60, 20)]]
        assert rows[0].source == "text:ocr"
        assert env.names("espresso") == ["receipt.jpg"]
        assert set(env.names("snowy field barn")) == {"receipt.jpg", "barn.jpg"}
    finally:
        env.runtime.close()


# --- the file page and the CLI -------------------------------------------------------


def test_the_file_page_shows_the_description_and_details(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fast_hashes(monkeypatch)
    make_owner(env.runtime)
    with TestClient(create_app(env.runtime), follow_redirects=False) as client:
        log_in(client)
        page = client.get("/ui/files/view", params={"path": f"lib:{env.paths['beach']}"})
        assert page.status_code == 200
        assert "Details" in page.text
        assert CAPTIONS[(10, 120, 200)] in page.text
        assert "2023-05-01 12:00 (UTC+02:00)" in page.text
        assert "47.50000, 8.25000" in page.text
        plain = client.get("/ui/files/view", params={"path": f"lib:{env.paths['icon']}"})
        assert "Details" not in plain.text
        hidden = client.get("/ui/files/view", params={"path": f"lib:{env.paths['secret']}"})
        assert hidden.status_code == 404


def test_files_info_prints_stages_and_properties(env: Env) -> None:
    from steward.cli import app

    env.runtime.close()
    result = CliRunner().invoke(
        app,
        [
            "--config-dir",
            str(env.tmp_path / "config"),
            "files",
            "info",
            f"lib:{env.paths['beach']}",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "photos.caption: done" in result.output
    assert "camera: Canon EOS R5" in result.output
    assert "image/jpeg (image)" in result.output
