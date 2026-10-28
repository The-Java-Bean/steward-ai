# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Web UI pages contributed by modules.

A module registers a FastAPI `APIRouter` under a path below `/ui/`. The core mounts it
behind the same checks as its own pages: a logged-in browser session is required (API
tokens can't open UI pages), and every POST/PUT/PATCH/DELETE must carry the session's
CSRF token (form field `csrf_token` or header `X-CSRF-Token`). Modules can't opt out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import APIRouter

UI_PATH_PATTERN = re.compile(r"^/ui/[a-z][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class UiPage:
    path: str
    """Where the router is mounted, e.g. `/ui/hello`; its routes are relative to it."""
    router: APIRouter
    title: str
    """Shown in the navigation."""
    nav: bool = True
    """Whether the page gets a navigation entry."""
