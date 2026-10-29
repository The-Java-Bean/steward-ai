# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Core index: the tags the AI chose per content, and indexes for property filters.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from steward.core.db.types import UTCDateTime

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tag_suggestions",
        sa.Column(
            "content_hash",
            sa.Text,
            sa.ForeignKey("blobs.content_hash", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("tag_id", sa.Integer, primary_key=True),
        sa.Column("created_at", UTCDateTime, nullable=False),
    )
    op.create_index("ix_tag_suggestions_tag", "tag_suggestions", ["tag_id"])
    op.create_index("ix_properties_key_text", "properties", ["key", "text"])
    op.create_index("ix_properties_key_num", "properties", ["key", "num"])


def downgrade() -> None:
    op.drop_index("ix_properties_key_num", "properties")
    op.drop_index("ix_properties_key_text", "properties")
    op.drop_table("tag_suggestions")
