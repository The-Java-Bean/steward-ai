# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Core index: baseline (tables arrive with discovery and the pipeline).

Revision ID: 0001
Revises:
Create Date: 2026-10-27
"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
