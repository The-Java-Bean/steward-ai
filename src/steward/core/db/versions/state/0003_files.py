# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Core state: files (discovery), folder inodes for rename detection.

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
    op.add_column("folders", sa.Column("inode", sa.Integer))
    op.create_index("ix_folders_inode", "folders", ["library_id", "inode"])
    op.create_table(
        "files",
        sa.Column("file_id", sa.Text, primary_key=True),
        sa.Column(
            "library_id",
            sa.Text,
            sa.ForeignKey("libraries.library_id", onupdate="CASCADE", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relpath", sa.Text, nullable=False),
        sa.Column("content_hash", sa.Text),
        sa.Column("hash_kind", sa.Text),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("mtime_ns", sa.Integer, nullable=False),
        sa.Column("inode", sa.Integer),
        sa.Column("present", sa.Boolean, nullable=False),
        sa.Column("first_seen", UTCDateTime, nullable=False),
        sa.Column("last_seen", UTCDateTime, nullable=False),
        sa.Column("missing_since", UTCDateTime),
        sa.Column("error", sa.Text),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
        sa.UniqueConstraint("library_id", "relpath", name="uq_files_location"),
        sa.CheckConstraint("hash_kind IN ('full', 'sampled')", name="ck_files_hash_kind"),
    )
    op.create_index("ix_files_content_hash", "files", ["content_hash"])
    op.create_index("ix_files_inode", "files", ["library_id", "inode"])
    op.create_index("ix_files_missing", "files", ["present", "missing_since"])


def downgrade() -> None:
    op.drop_table("files")
    op.drop_index("ix_folders_inode", "folders")
    with op.batch_alter_table("folders") as batch:
        batch.drop_column("inode")
