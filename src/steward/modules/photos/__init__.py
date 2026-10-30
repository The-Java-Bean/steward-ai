# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`photos`: metadata and descriptions of photos (plan §10.2-10.3, docs/adr/0024).

Stages:
- `photos.exif` (`mime` → `exif` properties; images and videos, also in metadata-only
  folders): the exiftool program reads the date taken, camera, lens, size,
  orientation, GPS position and duration. Registered only when `exiftool` is found;
  the health check `photos.exif` says when it isn't (installing it later back-fills).
- `photos.caption` (`mime` → `text:caption`; JPEG, PNG, WebP, GIF, TIFF, BMP): Pillow
  scales the image down in an isolated child process and the model mapped to the
  `caption` role (a vision model) describes it. `documents.chunk` chunks the
  description, so photos are found by words and by meaning like documents. Without a
  model mapped to `caption` the stage doesn't run.

Job `photos.sidecars` (`executor` lane, every 15 minutes) writes tags and descriptions to
XMP sidecars with the action `photos.sidecar_sync`, for photos in folders whose type has
`sidecars` (`media`). Needs `exiftool`; other programs' sidecars are merged, not
overwritten (docs/adr/0026).

Built only on `steward.sdk`. Originals are never changed.
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import ClassVar, cast

from pydantic import Field

from steward.modules.photos import caption, exif, images, sidecars
from steward.sdk import (
    BlobInfo,
    Every,
    FileInfo,
    FolderInfo,
    HealthResult,
    IsolationError,
    JobContext,
    ModuleConfig,
    ModuleKind,
    Registry,
    StageContext,
    StageResult,
    TextPart,
)

HERE = Path(__file__).parent
MIB = 1024 * 1024
SIDECARS_EVERY = timedelta(minutes=15)
EXIF_VERSION = 1
CAPTION_VERSION = 1
EXIF_STAGE = "photos.exif"
SIDECAR_JOB = "photos.sidecars"
ROLE = "caption"
CAPTION = "text:caption"

SKIP_REASONS = {
    "ExifFailed": "exiftool couldn't read it",
    "ExifTimedOut": "timed out",
    "TooSmall": "too small",
    "TooManyPixels": "too many pixels",
    "UnidentifiedImageError": "not a readable image",
}
ISOLATION_REASONS = {
    "timed_out": "timed out",
    "too_big": "needed too much memory",
    "crashed": "the reader crashed",
}


class PhotosConfig(ModuleConfig):
    exif: bool = True
    """Read photo and video metadata (needs the `exiftool` program)."""
    exif_command: str = "exiftool"
    """The exiftool program: a name looked up on PATH, or an absolute path."""
    exif_timeout_s: int = Field(30, ge=5, le=600)
    exif_memory_mib: int = Field(512, ge=128, le=8192)
    captions: bool = True
    """Describe photos with the model mapped to the `caption` role."""
    caption_max_file_mib: int = Field(100, ge=1, le=10_000)
    """Larger images aren't described."""
    caption_min_side: int = Field(256, ge=16, le=4096)
    """Images smaller than this on their shorter side (icons, thumbnails) aren't."""
    caption_max_side: int = Field(1024, ge=256, le=4096)
    """The image is scaled down to this before it's sent to the model."""
    caption_max_megapixels: int = Field(100, ge=1, le=1000)
    caption_max_chars: int = Field(1000, ge=100, le=10_000)
    caption_max_tokens: int = Field(512, ge=64, le=8192)
    """The longest answer the model may give (reasoning models need more)."""
    caption_timeout_s: int = Field(60, ge=5, le=3600)
    """Wall-clock time decoding and scaling one image may take."""
    caption_memory_mib: int = Field(1024, ge=256, le=65536)
    sidecars: bool = True
    """Write XMP sidecars in folders whose type has `sidecars` (needs `exiftool`)."""
    sidecar_batch: int = Field(50, ge=1, le=500)
    """Most sidecars written in one go (the write lock is held that long)."""
    sidecar_run_limit: int = Field(2000, ge=1, le=100_000)
    """Most sidecars written per run, so a first back-fill doesn't occupy the lane."""
    sidecar_max_kib: int = Field(1024, ge=16, le=65536)
    """Larger existing sidecars are left alone."""
    sidecar_timeout_s: int = Field(60, ge=5, le=600)
    """Time one exiftool run over a batch may take (plus a second per sidecar)."""


def _exif_version() -> int:
    return EXIF_VERSION * 100 + exif.VERSION


def _caption_version() -> int:
    return CAPTION_VERSION * 10_000 + images.VERSION * 100 + caption.PROMPT_VERSION


def _isolation_reason(exc: IsolationError) -> str:
    if exc.reason == "error":
        return SKIP_REASONS.get(exc.error_type or "", f"unreadable ({exc.error_type})")
    return ISOLATION_REASONS[exc.reason]


class PhotosModule:
    name: ClassVar[str] = "photos"
    version: ClassVar[str] = "0.2.0"
    kind: ClassVar[ModuleKind] = "feature"
    requires: ClassVar[tuple[str, ...]] = ()
    config_model: ClassVar[type[ModuleConfig] | None] = PhotosConfig

    def register(self, r: Registry) -> None:
        config = r.config if isinstance(r.config, PhotosConfig) else PhotosConfig()
        exiftool = shutil.which(config.exif_command)
        command = exiftool if config.exif else None
        r.migrations(HERE / "migrations" / "state")
        r.exporter(sidecars.export)

        def exif_applies(blob: BlobInfo, file: FileInfo, folder: FolderInfo) -> bool:
            return blob.kind in ("image", "video")

        def run_exif(ctx: StageContext) -> StageResult:
            fn = partial(
                exif.read_metadata,
                command=cast(str, command),
                timeout_s=config.exif_timeout_s * 0.9,
            )
            try:
                record = ctx.run_isolated(
                    fn, timeout_s=config.exif_timeout_s, memory_mib=config.exif_memory_mib
                )
            except IsolationError as exc:
                ctx.write_properties({}, artifact="exif")
                return StageResult("skipped", _isolation_reason(exc))
            values = exif.normalize(record)
            ctx.write_properties(values, artifact="exif")
            if not any(v is not None for v in values.values()):
                return StageResult("skipped", "no metadata")
            return StageResult()

        async def exif_health() -> HealthResult:
            if not config.exif:
                return HealthResult("ok", "photo metadata is turned off")
            if command is None:
                return HealthResult(
                    "warn",
                    f"photo metadata unavailable: {config.exif_command!r} isn't installed, "
                    "so dates, cameras and places of photos aren't read",
                )
            return HealthResult("ok", "exiftool")

        if command is not None:
            r.stage(
                EXIF_STAGE,
                run_exif,
                version=_exif_version(),
                requires=("mime",),
                produces=("exif",),
                lane="cpu",
                applies_to=exif_applies,
                index="metadata",
            )
        r.health_check(EXIF_STAGE, exif_health)

        if config.sidecars and exiftool is not None:
            syncer_of = partial(
                sidecars.Syncer,
                exiftool,
                config.sidecar_batch,
                config.sidecar_run_limit,
                config.sidecar_max_kib * 1024,
                config.sidecar_timeout_s,
            )

            def sync(ctx: JobContext) -> None:
                syncer_of().run(ctx)

            r.action(sidecars.SidecarSync())
            r.job(SIDECAR_JOB, sync, lane="executor")
            r.schedule(SIDECAR_JOB, Every(SIDECARS_EVERY), job=SIDECAR_JOB)

        if not config.captions:
            return
        r.model_role(
            ROLE,
            kind="chat",
            description="Describes photos so they can be searched (a vision model)",
            vision=True,
        )
        max_bytes = config.caption_max_file_mib * MIB

        def caption_applies(blob: BlobInfo, file: FileInfo, folder: FolderInfo) -> bool:
            return blob.mime in images.MIME_TYPES and blob.size <= max_bytes

        def run_caption(ctx: StageContext) -> StageResult:
            fn = partial(
                images.prepare,
                max_side=config.caption_max_side,
                min_side=config.caption_min_side,
                max_pixels=config.caption_max_megapixels * 1_000_000,
            )
            try:
                jpeg = ctx.run_isolated(
                    fn, timeout_s=config.caption_timeout_s, memory_mib=config.caption_memory_mib
                )
            except IsolationError as exc:
                ctx.write_text([], artifact=CAPTION)
                return StageResult("skipped", _isolation_reason(exc))
            answer = ctx.model(ROLE).chat(
                caption.messages(jpeg),
                options={"temperature": 0.2, "max_tokens": config.caption_max_tokens},
            )
            text = caption.clean(answer, config.caption_max_chars)
            ctx.write_text([TextPart(text)] if text else [], artifact=CAPTION)
            if not text:
                return StageResult(
                    "skipped", "the model gave no description", prompt_version=caption.PROMPT_NAME
                )
            return StageResult(prompt_version=caption.PROMPT_NAME)

        r.stage(
            "photos.caption",
            run_caption,
            version=_caption_version(),
            requires=("mime",),
            produces=(CAPTION,),
            lane="llm-local",
            applies_to=caption_applies,
            roles=(ROLE,),
        )
