# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""shares: share links, snapshots and the access log.

Revision ID: 0001
Revises:
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from steward.sdk.state import UTCDateTime

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shares_links",
        sa.Column("share_id", sa.Text, primary_key=True),
        sa.Column("token", sa.Text, nullable=False, unique=True),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("password_hash", sa.Text),
        sa.Column("expires_at", UTCDateTime),
        sa.Column("snapshot", sa.Boolean, nullable=False),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
        sa.Column("ended_at", UTCDateTime),
        sa.Column("end_reason", sa.Text),
        sa.Column("ended_by", sa.Text),
        sa.Column("access_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_access_at", UTCDateTime),
        sa.CheckConstraint("kind IN ('file', 'dir')", name="ck_shares_links_kind"),
        sa.CheckConstraint(
            "end_reason IS NULL OR end_reason IN ('revoked', 'expired', 'undone')",
            name="ck_shares_links_end_reason",
        ),
    )
    op.create_index("ix_shares_links_ended", "shares_links", ["ended_at", "expires_at"])
    op.create_table(
        "shares_snapshot",
        sa.Column(
            "share_id",
            sa.Text,
            sa.ForeignKey("shares_links.share_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("file_id", sa.Text, primary_key=True),
    )
    op.create_table(
        "shares_access",
        sa.Column("access_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "share_id",
            sa.Text,
            sa.ForeignKey("shares_links.share_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("at", UTCDateTime, nullable=False),
        sa.Column("ip", sa.Text, nullable=False),
        sa.Column("user_agent", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.CheckConstraint(
            "kind IN ('view', 'download', 'zip', 'password_failed')",
            name="ck_shares_access_kind",
        ),
    )
    op.create_index("ix_shares_access_share", "shares_access", ["share_id", "at"])


def downgrade() -> None:
    op.drop_table("shares_access")
    op.drop_table("shares_snapshot")
    op.drop_table("shares_links")
