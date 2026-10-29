# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`shares`: the built-in share links (plan §14.4, docs/adr/0020).

Public links at `/s/<token>` to a file or a folder (preview, download, folder listing,
zip), with an expiry (7 days by default; no expiry is allowed but needs the password and
is highlighted) and a password (on by default). `/ui/shares` lists every share, active
and past, with its access log; the file manager links to the share dialog. The core
re-checks on every request that the item may still be shared (not locked, not
`local-only` or `excluded`, in a shareable folder). Built only on `steward.sdk`.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import ClassVar

from steward.modules.shares import public, store
from steward.modules.shares.actions import register_actions
from steward.modules.shares.routes import PAGE, router
from steward.sdk import Every, JobContext, ModuleConfig, ModuleKind, Registry, utcnow

HERE = Path(__file__).parent
EXPIRE_EVERY = timedelta(minutes=15)


def expire(ctx: JobContext) -> None:
    """Record shares whose time ran out as ended (links stop at their expiry anyway)."""
    store.expire_due(ctx.state, now=utcnow())


class SharesModule:
    name: ClassVar[str] = "shares"
    version: ClassVar[str] = "0.1.0"
    kind: ClassVar[ModuleKind] = "feature"
    requires: ClassVar[tuple[str, ...]] = ()
    config_model: ClassVar[type[ModuleConfig] | None] = None

    def register(self, r: Registry) -> None:
        r.migrations(HERE / "migrations" / "state")
        register_actions(r)
        r.ui_page(
            "shares.page",
            router,
            path=PAGE,
            title="Shares",
            templates=HERE / "templates",
            static=HERE / "static",
            badge=store.count_no_expiry,
        )
        r.share_links(public.handle)
        r.job("shares.expire", expire, lane="io")
        r.schedule("shares.expire", Every(EXPIRE_EVERY), job="shares.expire")
        r.exporter(store.export)
