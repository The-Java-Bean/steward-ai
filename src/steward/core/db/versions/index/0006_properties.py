# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Core index: properties, structured metadata per content (docs/adr/0024).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from steward.core.db.types import UTCDateTime

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "properties",
        sa.Column(
            "content_hash",
            sa.Text,
            sa.ForeignKey("blobs.content_hash", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("artifact", sa.Text, primary_key=True),
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("text", sa.Text),
        sa.Column("num", sa.Float),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.CheckConstraint("type IN ('text', 'int', 'float', 'bool')", name="ck_properties_type"),
    )


def downgrade() -> None:
    op.drop_table("properties")
