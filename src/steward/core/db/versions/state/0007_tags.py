# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Core state: the tag vocabulary and the tags you added to or removed from files.

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
        "tags",
        sa.Column("tag_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("name_key", sa.Text, nullable=False, unique=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("target_id", sa.Integer, sa.ForeignKey("tags.tag_id", ondelete="CASCADE")),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'ai-created', 'deprecated', 'alias')", name="ck_tags_status"
        ),
        sa.CheckConstraint(
            "(status = 'alias') = (target_id IS NOT NULL)", name="ck_tags_alias_target"
        ),
    )
    op.create_index("ix_tags_target", "tags", ["target_id"])
    op.create_table(
        "file_tags",
        sa.Column(
            "file_id",
            sa.Text,
            sa.ForeignKey("files.file_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tag_id",
            sa.Integer,
            sa.ForeignKey("tags.tag_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("changed_at", UTCDateTime, nullable=False),
        sa.CheckConstraint("state IN ('added', 'removed')", name="ck_file_tags_state"),
    )
    op.create_index("ix_file_tags_tag", "file_tags", ["tag_id", "state"])


def downgrade() -> None:
    op.drop_table("file_tags")
    op.drop_table("tags")
