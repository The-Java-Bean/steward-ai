# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Core state: libraries, folder types, folders, mappings, roles, path history.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-28
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from steward.core.db.types import UTCDateTime

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Built-in types at the time of this revision. Their settings live in code; these
# rows exist so mappings can reference them and so the owner's overrides have a place.
BUILTIN_TYPES = (
    "general",
    "inbox",
    "documents",
    "media",
    "notes",
    "projects",
    "archive",
    "software",
    "games",
    "private",
    "trash",
    "app-managed",
    "ignored",
)


def upgrade() -> None:
    op.create_table(
        "libraries",
        sa.Column("library_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("root", sa.Text, nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
    )
    folder_types = op.create_table(
        "folder_types",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("builtin", sa.Boolean, nullable=False),
        sa.Column(
            "extends",
            sa.Text,
            sa.ForeignKey("folder_types.name", onupdate="CASCADE", ondelete="RESTRICT"),
        ),
        sa.Column("settings", sa.JSON, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
        sa.CheckConstraint(
            "builtin = 0 OR extends IS NULL", name="ck_folder_types_builtin_extends"
        ),
    )
    op.create_table(
        "folders",
        sa.Column("folder_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "library_id",
            sa.Text,
            sa.ForeignKey("libraries.library_id", onupdate="CASCADE", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relpath", sa.Text, nullable=False),
        sa.Column("present", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("first_seen", UTCDateTime),
        sa.Column("last_seen", UTCDateTime),
        sa.Column("missing_since", UTCDateTime),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
        sa.UniqueConstraint("library_id", "relpath", name="uq_folders_location"),
    )
    op.create_table(
        "folder_mappings",
        sa.Column(
            "folder_id",
            sa.Integer,
            sa.ForeignKey("folders.folder_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "type_name",
            sa.Text,
            sa.ForeignKey("folder_types.name", onupdate="CASCADE", ondelete="RESTRICT"),
        ),
        sa.Column("locked", sa.Boolean),
        sa.Column("permanent", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
        sa.CheckConstraint("permanent = 0 OR locked = 1", name="ck_folder_mappings_permanent"),
    )
    op.create_index("ix_folder_mappings_type", "folder_mappings", ["type_name"])
    op.create_table(
        "folder_roles",
        sa.Column("role", sa.Text, primary_key=True),
        sa.Column(
            "folder_id",
            sa.Integer,
            sa.ForeignKey("folders.folder_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
        sa.CheckConstraint(
            "role IN ('trash', 'edit_backups', 'inbox', 'digest_output')",
            name="ck_folder_roles_role",
        ),
    )
    op.create_table(
        "path_history",
        sa.Column("history_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("folder_id", sa.Integer),
        sa.Column("file_id", sa.Text),
        sa.Column("old_library_id", sa.Text, nullable=False),
        sa.Column("old_relpath", sa.Text, nullable=False),
        sa.Column("new_library_id", sa.Text, nullable=False),
        sa.Column("new_relpath", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("changed_at", UTCDateTime, nullable=False),
        sa.CheckConstraint(
            "(folder_id IS NULL) != (file_id IS NULL)", name="ck_path_history_one_entity"
        ),
    )
    op.create_index("ix_path_history_folder", "path_history", ["folder_id"])
    op.create_index("ix_path_history_file", "path_history", ["file_id"])

    now = datetime.now(UTC)
    op.bulk_insert(
        folder_types,
        [
            {
                "name": name,
                "builtin": True,
                "extends": None,
                "settings": {},
                "actor": "system",
                "created_at": now,
                "updated_at": now,
            }
            for name in BUILTIN_TYPES
        ],
    )


def downgrade() -> None:
    op.drop_table("path_history")
    op.drop_table("folder_roles")
    op.drop_table("folder_mappings")
    op.drop_table("folders")
    op.drop_table("folder_types")
    op.drop_table("libraries")
