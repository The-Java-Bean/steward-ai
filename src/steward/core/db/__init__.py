# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Databases: `state.db` (irreplaceable) and `index.db` (rebuildable); docs/adr/0001."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from steward.core.db.engine import Database
from steward.core.db.types import UTCDateTime, utcnow


@dataclass(frozen=True)
class Databases:
    state: Database
    index: Database

    @classmethod
    def open(cls, data_dir: Path) -> Databases:
        return cls(
            state=Database(data_dir / "state.db", name="state"),
            index=Database(data_dir / "index.db", name="index"),
        )

    def dispose(self) -> None:
        self.state.dispose()
        self.index.dispose()


__all__ = ["Database", "Databases", "UTCDateTime", "utcnow"]
