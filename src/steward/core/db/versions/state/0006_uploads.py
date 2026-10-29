# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Core state: resumable upload sessions.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-28
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
        "uploads",
        sa.Column("upload_id", sa.Text, primary_key=True),
        sa.Column("library_id", sa.Text, nullable=False),
        sa.Column("folder", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("received", sa.Integer, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("confirmed", sa.Boolean, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("entry_id", sa.Integer, sa.ForeignKey("journal.entry_id", ondelete="SET NULL")),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
        sa.CheckConstraint(
            "status IN ('open', 'finished', 'cancelled', 'expired')", name="ck_uploads_status"
        ),
    )
    op.create_index("ix_uploads_status", "uploads", ["status", "updated_at"])


def downgrade() -> None:
    op.drop_table("uploads")
