# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Core tables, as SQLAlchemy Core metadata. Migrations create them; a test checks
that the migrations and these definitions agree."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)

from steward.core.db.types import UTCDateTime

state_metadata = MetaData()
index_metadata = MetaData()

JOB_STATUSES = ("queued", "running", "done", "dead")
ACTIVE_JOB_STATUSES = ("queued", "running")

jobs = Table(
    "jobs",
    state_metadata,
    Column("job_id", Integer, primary_key=True, autoincrement=True),
    Column("type", Text, nullable=False),
    Column("lane", Text, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("priority", Integer, nullable=False, server_default=text("0")),
    Column("status", Text, nullable=False),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False),
    Column("run_after", UTCDateTime, nullable=False),
    Column("lease_until", UTCDateTime),
    Column("worker_id", Text),
    Column("last_error", Text),
    Column("dedupe_key", Text),
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    Column("finished_at", UTCDateTime),
    CheckConstraint("status IN ('queued', 'running', 'done', 'dead')", name="ck_jobs_status"),
    Index("ix_jobs_claim", "lane", "status", "run_after", "priority"),
    Index("ix_jobs_finished", "status", "finished_at"),
    Index(
        "ux_jobs_active_dedupe",
        "dedupe_key",
        unique=True,
        sqlite_where=text("status IN ('queued', 'running')"),
    ),
)

events = Table(
    "events",
    state_metadata,
    Column("event_id", Integer, primary_key=True, autoincrement=True),
    Column("type", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("actor", Text, nullable=False),
    Column("occurred_at", UTCDateTime, nullable=False),
    Index("ix_events_type", "type", "event_id"),
    Index("ix_events_occurred", "occurred_at"),
)

event_cursors = Table(
    "event_cursors",
    state_metadata,
    Column("subscriber", Text, primary_key=True),
    Column("last_event_id", Integer, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
)

schedules = Table(
    "schedules",
    state_metadata,
    Column("name", Text, primary_key=True),
    Column("spec", Text, nullable=False),
    Column("last_run_at", UTCDateTime),
    Column("next_run_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
)

# --- libraries, folders, folder types (revision 0002) ---------------------------

ROLES = ("trash", "edit_backups", "inbox", "digest_output")

libraries = Table(
    "libraries",
    state_metadata,
    Column("library_id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("root", Text, nullable=False),
    Column("position", Integer, nullable=False),
    Column("actor", Text, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
)

folder_types = Table(
    "folder_types",
    state_metadata,
    Column("name", Text, primary_key=True),
    Column("builtin", Boolean, nullable=False),
    Column(
        "extends",
        Text,
        ForeignKey("folder_types.name", onupdate="CASCADE", ondelete="RESTRICT"),
    ),
    Column("settings", JSON, nullable=False),
    Column("actor", Text, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    CheckConstraint("builtin = 0 OR extends IS NULL", name="ck_folder_types_builtin_extends"),
)

folders = Table(
    "folders",
    state_metadata,
    Column("folder_id", Integer, primary_key=True, autoincrement=True),
    Column(
        "library_id",
        Text,
        ForeignKey("libraries.library_id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("relpath", Text, nullable=False),
    Column("present", Boolean, nullable=False, server_default=text("0")),
    Column("first_seen", UTCDateTime),
    Column("last_seen", UTCDateTime),
    Column("missing_since", UTCDateTime),
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    Column("inode", Integer),  # revision 0003
    UniqueConstraint("library_id", "relpath", name="uq_folders_location"),
    Index("ix_folders_inode", "library_id", "inode"),
)

folder_mappings = Table(
    "folder_mappings",
    state_metadata,
    Column(
        "folder_id",
        Integer,
        ForeignKey("folders.folder_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "type_name",
        Text,
        ForeignKey("folder_types.name", onupdate="CASCADE", ondelete="RESTRICT"),
    ),
    Column("locked", Boolean),
    Column("permanent", Boolean, nullable=False, server_default=text("0")),
    Column("actor", Text, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    CheckConstraint("permanent = 0 OR locked = 1", name="ck_folder_mappings_permanent"),
    Index("ix_folder_mappings_type", "type_name"),
)

folder_roles = Table(
    "folder_roles",
    state_metadata,
    Column("role", Text, primary_key=True),
    Column(
        "folder_id",
        Integer,
        ForeignKey("folders.folder_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("actor", Text, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    CheckConstraint(
        "role IN ('trash', 'edit_backups', 'inbox', 'digest_output')", name="ck_folder_roles_role"
    ),
)

path_history = Table(
    "path_history",
    state_metadata,
    Column("history_id", Integer, primary_key=True, autoincrement=True),
    Column("folder_id", Integer),
    Column("file_id", Text),
    Column("old_library_id", Text, nullable=False),
    Column("old_relpath", Text, nullable=False),
    Column("new_library_id", Text, nullable=False),
    Column("new_relpath", Text, nullable=False),
    Column("actor", Text, nullable=False),
    Column("changed_at", UTCDateTime, nullable=False),
    CheckConstraint("(folder_id IS NULL) != (file_id IS NULL)", name="ck_path_history_one_entity"),
    Index("ix_path_history_folder", "folder_id"),
    Index("ix_path_history_file", "file_id"),
)
"""Every path a folder or file has had. A folder move records one row per folder in the
moved subtree; files inside it follow without rows of their own (their old paths are
implied by the folder's)."""

# --- discovery (state revision 0003) -----------------------------------------------

files = Table(
    "files",
    state_metadata,
    Column("file_id", Text, primary_key=True),
    Column(
        "library_id",
        Text,
        ForeignKey("libraries.library_id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("relpath", Text, nullable=False),
    Column("content_hash", Text),
    Column("hash_kind", Text),
    Column("size", Integer, nullable=False),
    Column("mtime_ns", Integer, nullable=False),
    Column("inode", Integer),
    Column("present", Boolean, nullable=False),
    Column("first_seen", UTCDateTime, nullable=False),
    Column("last_seen", UTCDateTime, nullable=False),
    Column("missing_since", UTCDateTime),
    Column("error", Text),
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    UniqueConstraint("library_id", "relpath", name="uq_files_location"),
    CheckConstraint("hash_kind IN ('full', 'sampled')", name="ck_files_hash_kind"),
    Index("ix_files_content_hash", "content_hash"),
    Index("ix_files_inode", "library_id", "inode"),
    Index("ix_files_missing", "present", "missing_since"),
)

# --- index.db ----------------------------------------------------------------------

blobs = Table(
    "blobs",
    index_metadata,
    Column("content_hash", Text, primary_key=True),
    Column("hash_kind", Text, nullable=False),
    Column("size", Integer, nullable=False),
    Column("mime", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    CheckConstraint("hash_kind IN ('full', 'sampled')", name="ck_blobs_hash_kind"),
)

STAGE_RUN_STATUSES = ("done", "failed", "skipped")

stage_runs = Table(
    "stage_runs",
    index_metadata,
    Column(
        "content_hash",
        Text,
        ForeignKey("blobs.content_hash", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("stage", Text, primary_key=True),
    Column("stage_version", Integer, nullable=False),
    Column("model", Text),
    Column("prompt_version", Text),
    Column("status", Text, nullable=False),
    Column("error", Text),
    Column("updated_at", UTCDateTime, nullable=False),
    CheckConstraint("status IN ('done', 'failed', 'skipped')", name="ck_stage_runs_status"),
    Index("ix_stage_runs_stage", "stage", "status"),
)

pipeline_meta = Table(
    "pipeline_meta",
    index_metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
)
"""Small facts about the pipeline, e.g. the stage graph signature of the last full
planning pass (index revision 0003)."""

# --- auth and runtime settings (state revision 0004) -------------------------------

USER_ROLES = ("owner",)

users = Table(
    "users",
    state_metadata,
    Column("user_id", Integer, primary_key=True, autoincrement=True),
    Column("username", Text, nullable=False, unique=True),
    Column("role", Text, nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("password_changed_at", UTCDateTime, nullable=False),
    Column("disabled_at", UTCDateTime),
    Column("actor", Text, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    CheckConstraint("role IN ('owner')", name="ck_users_role"),
)
"""`username` is stored normalized (NFKC, lower case), so uniqueness ignores case."""

sessions = Table(
    "sessions",
    state_metadata,
    Column("session_id", Integer, primary_key=True, autoincrement=True),
    Column("token_hash", Text, nullable=False, unique=True),
    Column("user_id", Integer, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("last_seen", UTCDateTime, nullable=False),
    Column("reauth_at", UTCDateTime),
    Column("ip", Text),
    Column("user_agent", Text),
    Column("revoked_at", UTCDateTime),
    Column("revoked_reason", Text),
    Index("ix_sessions_user", "user_id", "revoked_at"),
)
"""Server-side sessions. The cookie holds a random secret; only its SHA-256 is stored."""

api_tokens = Table(
    "api_tokens",
    state_metadata,
    Column("token_id", Integer, primary_key=True, autoincrement=True),
    Column("secret_hash", Text, nullable=False, unique=True),
    Column("hint", Text, nullable=False),
    Column("user_id", Integer, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
    Column("label", Text, nullable=False),
    Column("scopes", JSON, nullable=False),
    Column("expires_at", UTCDateTime),
    Column("last_used", UTCDateTime),
    Column("last_ip", Text),
    Column("revoked_at", UTCDateTime),
    Column("actor", Text, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Index("ix_api_tokens_user", "user_id", "revoked_at"),
)
"""Scoped API tokens (`read`, `propose`). Only the SHA-256 of the token is stored;
`hint` is its last characters, for telling tokens apart in lists."""

LOGIN_ATTEMPT_KINDS = ("login", "reauth")

login_attempts = Table(
    "login_attempts",
    state_metadata,
    Column("attempt_id", Integer, primary_key=True, autoincrement=True),
    Column("username", Text, nullable=False),
    Column("ip", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("success", Boolean, nullable=False),
    Column("at", UTCDateTime, nullable=False),
    CheckConstraint("kind IN ('login', 'reauth')", name="ck_login_attempts_kind"),
    Index("ix_login_attempts_ip", "ip", "at"),
    Index("ix_login_attempts_username", "username", "at"),
    Index("ix_login_attempts_at", "at"),
)
"""Password checks, for rate limiting and the health page. Never holds a password."""

settings_kv = Table(
    "settings_kv",
    state_metadata,
    Column("key", Text, primary_key=True),
    Column("value", JSON, nullable=False),
    Column("actor", Text, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
)
"""Runtime settings edited in the UI (`auth.reauth_minutes`, ...). Keys are
`<owner>.<name>`; a missing key means the default."""

# --- privacy overrides and the journal (state revision 0005) ------------------------

file_sensitivity = Table(
    "file_sensitivity",
    state_metadata,
    Column(
        "file_id",
        Text,
        ForeignKey("files.file_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("sensitivity", Text, nullable=False),
    Column("actor", Text, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    CheckConstraint("sensitivity IN ('local-only', 'excluded')", name="ck_file_sensitivity_level"),
)
"""Per-file overrides that raise a file above its folder's sensitivity."""

journal = Table(
    "journal",
    state_metadata,
    Column("entry_id", Integer, primary_key=True, autoincrement=True),
    Column("action", Text, nullable=False),
    Column("params", JSON, nullable=False),
    Column("plan", JSON, nullable=False),
    Column("result", JSON, nullable=False),
    Column("inverse", JSON),
    Column("summary", Text, nullable=False),
    Column("sensitive", Boolean, nullable=False),
    Column("actor", Text, nullable=False),
    Column("proposal_id", Integer),
    Column("undo_of", Integer, ForeignKey("journal.entry_id", ondelete="RESTRICT")),
    Column("undone_by", Integer, ForeignKey("journal.entry_id", ondelete="RESTRICT")),
    Column("applied_at", UTCDateTime, nullable=False),
    Index("ix_journal_applied", "applied_at"),
)
"""Every applied action with what it did (`result`, including the state it left the
files in) and its undo plan (`inverse`, None if it can't be undone). Rows are never
deleted."""

uploads = Table(
    "uploads",
    state_metadata,
    Column("upload_id", Text, primary_key=True),
    Column("library_id", Text, nullable=False),
    Column("folder", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("size", Integer, nullable=False),
    Column("received", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("confirmed", Boolean, nullable=False),
    Column("actor", Text, nullable=False),
    Column("entry_id", Integer, ForeignKey("journal.entry_id", ondelete="SET NULL")),
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    CheckConstraint(
        "status IN ('open', 'finished', 'cancelled', 'expired')", name="ck_uploads_status"
    ),
    Index("ix_uploads_status", "status", "updated_at"),
)
"""Resumable uploads (`core.executor.uploads`): the bytes so far live in
`.steward-tmp-up-<upload_id>` inside `library_id:folder`; `received` is how many are
written (always a chunk boundary). Finished rows point at the `file.upload` entry."""
