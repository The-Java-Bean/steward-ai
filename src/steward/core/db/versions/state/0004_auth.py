# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Core state: users, sessions, API tokens, login attempts, runtime settings.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from steward.core.db.types import UTCDateTime

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("username", sa.Text, nullable=False, unique=True),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("password_changed_at", UTCDateTime, nullable=False),
        sa.Column("disabled_at", UTCDateTime),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
        sa.CheckConstraint("role IN ('owner')", name="ck_users_role"),
    )
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.Text, nullable=False, unique=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.Column("last_seen", UTCDateTime, nullable=False),
        sa.Column("reauth_at", UTCDateTime),
        sa.Column("ip", sa.Text),
        sa.Column("user_agent", sa.Text),
        sa.Column("revoked_at", UTCDateTime),
        sa.Column("revoked_reason", sa.Text),
    )
    op.create_index("ix_sessions_user", "sessions", ["user_id", "revoked_at"])
    op.create_table(
        "api_tokens",
        sa.Column("token_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("secret_hash", sa.Text, nullable=False, unique=True),
        sa.Column("hint", sa.Text, nullable=False),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("scopes", sa.JSON, nullable=False),
        sa.Column("expires_at", UTCDateTime),
        sa.Column("last_used", UTCDateTime),
        sa.Column("last_ip", sa.Text),
        sa.Column("revoked_at", UTCDateTime),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("created_at", UTCDateTime, nullable=False),
    )
    op.create_index("ix_api_tokens_user", "api_tokens", ["user_id", "revoked_at"])
    op.create_table(
        "login_attempts",
        sa.Column("attempt_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("username", sa.Text, nullable=False),
        sa.Column("ip", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("success", sa.Boolean, nullable=False),
        sa.Column("at", UTCDateTime, nullable=False),
        sa.CheckConstraint("kind IN ('login', 'reauth')", name="ck_login_attempts_kind"),
    )
    op.create_index("ix_login_attempts_ip", "login_attempts", ["ip", "at"])
    op.create_index("ix_login_attempts_username", "login_attempts", ["username", "at"])
    op.create_index("ix_login_attempts_at", "login_attempts", ["at"])
    op.create_table(
        "settings_kv",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.JSON, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("settings_kv")
    op.drop_table("login_attempts")
    op.drop_table("api_tokens")
    op.drop_table("sessions")
    op.drop_table("users")
