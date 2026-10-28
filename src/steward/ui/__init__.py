# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Templates and static files for the web UI (server-rendered, no JavaScript yet)."""

from pathlib import Path

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

__all__ = ["STATIC_DIR", "TEMPLATES_DIR"]
