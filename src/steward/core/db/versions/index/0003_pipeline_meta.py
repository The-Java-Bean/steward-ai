# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Core index: pipeline_meta (stage graph signature of the last full planning pass).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from steward.core.db.types import UTCDateTime

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pipeline_meta",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("pipeline_meta")
