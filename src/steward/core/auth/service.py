# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Accounts, logins, sessions, re-auth, API tokens and login settings (docs/adr/0012).

`Auth` holds no state of its own besides its config; every method opens its own
transactions on `state.db`. Password hashing always happens outside a transaction, so
the write lock is never held for the tens of milliseconds a hash takes. Times can be
passed in (`now=`) for tests; they default to the current time.

Who may do what:
- A **session** (a logged-in browser) may do everything the owner may, but sensitive
  steps also need a recent password check (`require_recent_auth`).
- An **API token** may only do what its scopes say (`read`, `propose`). It can never
  approve proposals, open locked folders, change settings or create tokens.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Connection, Row, func, or_, select, update

from steward.core import events, settings
from steward.core.auth import limits, passwords, tokens
from steward.core.auth.limits import AttemptKind, Lockout
from steward.core.config import AuthConfig
from steward.core.db import Database, utcnow
from steward.core.db.schema import api_tokens, sessions, users

log = logging.getLogger(__name__)

USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._@-]{0,63}$")
MAX_USER_AGENT = 512
MAX_LABEL = 100
MAX_SECRET = 256
LAST_SEEN_RESOLUTION = timedelta(minutes=1)
"""`last_seen`/`last_used` are written at most this often per session or token."""
ONE_SHOT_REAUTH = timedelta(minutes=2)
"""With a re-auth window of 0, a password check allows one sensitive step this soon after."""

type TokenScope = Literal["read", "propose"]
TOKEN_SCOPES: tuple[TokenScope, ...] = get_args(TokenScope.__value__)
type Via = Literal["session", "token"]


class AuthError(Exception):
    """Base class; messages are safe to show to the person logging in."""


class LoginFailed(AuthError):
    """Wrong username or password. Deliberately doesn't say which."""


class LockedOut(AuthError):
    def __init__(self, lockout: Lockout, now: datetime) -> None:
        self.lockout = lockout
        self.retry_after = lockout.retry_after(now)
        super().__init__(f"too many failed attempts; try again in {_minutes(self.retry_after)}")


class ReauthRequired(AuthError):
    """Enter the password again before this step."""


class NotAllowed(AuthError):
    """This principal can't do this at all (e.g. an API token creating tokens)."""


class AccountError(AuthError):
    """A username is invalid or taken, there's no such user, or setup already happened."""


def _minutes(seconds: int) -> str:
    minutes = -(-seconds // 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def normalize_username(username: str) -> str:
    """NFKC, trimmed, case-folded. Raises `AccountError` if the result isn't allowed."""
    name = unicodedata.normalize("NFKC", username).strip().casefold()
    if not USERNAME_PATTERN.match(name):
        raise AccountError(
            "usernames are 1-64 characters: letters, digits, '.', '_', '@' or '-', "
            "starting with a letter or digit"
        )
    return name


# --- value objects -----------------------------------------------------------------


@dataclass(frozen=True)
class User:
    user_id: int
    username: str
    role: str
    created_at: datetime
    password_changed_at: datetime
    disabled_at: datetime | None

    @property
    def actor(self) -> str:
        return f"user:{self.user_id}"


@dataclass(frozen=True)
class SessionInfo:
    session_id: int
    user_id: int
    created_at: datetime
    last_seen: datetime
    reauth_at: datetime | None
    ip: str | None
    user_agent: str | None
    revoked_at: datetime | None
    revoked_reason: str | None
    expires_at: datetime
    """When it ends without further use: the earlier of idle timeout and max lifetime."""

    def active(self, now: datetime) -> bool:
        return self.revoked_at is None and now < self.expires_at


@dataclass(frozen=True)
class TokenInfo:
    token_id: int
    user_id: int
    label: str
    scopes: tuple[TokenScope, ...]
    hint: str
    created_at: datetime
    expires_at: datetime | None
    last_used: datetime | None
    last_ip: str | None
    revoked_at: datetime | None

    def active(self, now: datetime) -> bool:
        return self.revoked_at is None and (self.expires_at is None or now < self.expires_at)


@dataclass(frozen=True)
class Principal:
    """Who is making a request, and through what."""

    user: User
    via: Via
    session_id: int | None = None
    token_id: int | None = None
    scopes: frozenset[TokenScope] = frozenset()

    @property
    def actor(self) -> str:
        return self.user.actor

    @property
    def is_session(self) -> bool:
        return self.via == "session"

    def can(self, scope: TokenScope) -> bool:
        return self.is_session or scope in self.scopes


@dataclass(frozen=True)
class NewSession:
    secret: str
    """Goes in the session cookie; never stored or logged."""
    principal: Principal


class AuthSettings(BaseModel):
    """Login settings edited in the UI (`settings_kv`, keys `auth.<field>`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reauth_enabled: bool = True
    """Off: sensitive steps and locked folders need only a normal login."""
    reauth_minutes: int = Field(15, ge=0, le=24 * 60)
    """How long a password check counts as recent. 0: ask before every sensitive step."""


# --- the service -------------------------------------------------------------------


class Auth:
    def __init__(self, db: Database, config: AuthConfig) -> None:
        self.db = db
        self.config = config
        self.idle = timedelta(hours=config.session_idle_hours)
        self.max_age = timedelta(days=config.session_max_days)

    # --- accounts ----------------------------------------------------------------

    def has_owner(self) -> bool:
        with self.db.read() as conn:
            return _owner_count(conn) > 0

    def create_owner(
        self, username: str, password: str, *, actor: str = "setup", now: datetime | None = None
    ) -> User:
        """Create the one owner account. Fails if one exists."""
        name = normalize_username(username)
        passwords.check(password, username=name)
        password_hash = passwords.hash_password(password)
        now = now or utcnow()
        with self.db.write() as conn:
            if _owner_count(conn):
                raise AccountError("an owner account already exists")
            user_id = _insert_id(
                conn.execute(
                    users.insert().values(
                        username=name,
                        role="owner",
                        password_hash=password_hash,
                        password_changed_at=now,
                        actor=actor,
                        created_at=now,
                        updated_at=now,
                    )
                )
            )
            events.emit(
                conn,
                "auth.user_created",
                {"user_id": user_id, "username": name, "role": "owner"},
                actor=actor,
                now=now,
            )
            return _user(_load_user(conn, user_id=user_id))

    def get_user(self, *, username: str | None = None, user_id: int | None = None) -> User:
        with self.db.read() as conn:
            row = _load_user(
                conn,
                username=normalize_username(username) if username is not None else None,
                user_id=user_id,
            )
        if row is None:
            raise AccountError("no such user")
        return _user(row)

    def list_users(self) -> list[User]:
        with self.db.read() as conn:
            return [_user(r) for r in conn.execute(select(users).order_by(users.c.user_id))]

    def reset_password(
        self, username: str, new_password: str, *, actor: str = "cli", now: datetime | None = None
    ) -> int:
        """Host-side recovery: set a new password, end every session, clear lockouts.
        Returns the number of sessions ended."""
        name = normalize_username(username)
        passwords.check(new_password, username=name)
        password_hash = passwords.hash_password(new_password)
        now = now or utcnow()
        with self.db.write() as conn:
            row = _load_user(conn, username=name)
            if row is None:
                raise AccountError("no such user")
            _set_password(conn, row.user_id, password_hash, now)
            ended = _revoke_sessions(
                conn, row.user_id, reason="password reset", actor=actor, now=now
            )
            limits.clear(conn, username=name)
            events.emit(
                conn,
                "auth.password_changed",
                {"user_id": row.user_id, "sessions_ended": ended, "reset": True},
                actor=actor,
                now=now,
            )
        log.info("password reset", extra={"fields": {"user_id": row.user_id, "actor": actor}})
        return ended

    def unlock(self, *, username: str | None = None, ip: str | None = None) -> int:
        """Lift lockouts by forgetting recent failures. Returns how many were forgotten."""
        name = normalize_username(username) if username is not None else None
        with self.db.write() as conn:
            return limits.clear(conn, username=name, ip=ip)

    # --- logging in and out ------------------------------------------------------

    def login(
        self,
        username: str,
        password: str,
        *,
        ip: str,
        user_agent: str | None = None,
        now: datetime | None = None,
    ) -> NewSession:
        """Check the password and start a session. Raises `LockedOut` or `LoginFailed`."""
        now = now or utcnow()
        try:
            name = normalize_username(username)
        except AccountError:
            name = unicodedata.normalize("NFKC", username).strip().casefold()[:64]
        with self.db.read() as conn:
            lockout = limits.check(conn, self.config.login, username=name, ip=ip, now=now)
            row = _load_user(conn, username=name) if lockout is None else None
        if lockout is not None:
            log.warning(
                "login refused during lockout",
                extra={"fields": {"username": name, "ip": ip, "scope": lockout.scope}},
            )
            raise LockedOut(lockout, now)

        if row is None or row.disabled_at is not None:
            passwords.dummy_verify(password)
            ok = False
        else:
            ok = passwords.verify_password(row.password_hash, password)
        if not ok:
            self._failed(name, ip, "login", now)
            raise LoginFailed("wrong username or password")
        assert row is not None  # noqa: S101 -- ok implies a user row

        new_hash = (
            passwords.hash_password(password) if passwords.needs_rehash(row.password_hash) else None
        )
        secret = tokens.new_session_secret()
        with self.db.write() as conn:
            limits.record(conn, username=name, ip=ip, kind="login", success=True, now=now)
            if new_hash is not None:
                conn.execute(
                    update(users)
                    .where(users.c.user_id == row.user_id)
                    .values(password_hash=new_hash, updated_at=now)
                )
            session_id = _insert_id(
                conn.execute(
                    sessions.insert().values(
                        token_hash=tokens.digest(secret),
                        user_id=row.user_id,
                        created_at=now,
                        last_seen=now,
                        reauth_at=now,
                        ip=ip,
                        user_agent=(user_agent or "")[:MAX_USER_AGENT] or None,
                    )
                )
            )
            events.emit(
                conn,
                "auth.login",
                {"user_id": row.user_id, "session_id": session_id, "ip": ip},
                actor=f"user:{row.user_id}",
                now=now,
            )
        log.info(
            "login", extra={"fields": {"user_id": row.user_id, "session_id": session_id, "ip": ip}}
        )
        return NewSession(secret, Principal(_user(row), "session", session_id=session_id))

    def _failed(self, name: str, ip: str, kind: AttemptKind, now: datetime) -> None:
        with self.db.write() as conn:
            limits.record(conn, username=name, ip=ip, kind=kind, success=False, now=now)
            events.emit(
                conn,
                "auth.login_failed",
                {"username": name, "ip": ip, "kind": kind},
                actor="system",
                now=now,
            )
            lockout = limits.check(conn, self.config.login, username=name, ip=ip, now=now)
            if lockout is not None:
                events.emit(
                    conn,
                    "auth.locked_out",
                    {
                        "scope": lockout.scope,
                        "username": name,
                        "ip": ip,
                        "until": lockout.until.isoformat(),
                    },
                    actor="system",
                    now=now,
                )
        log.warning(
            "password check failed",
            extra={
                "fields": {
                    "username": name,
                    "ip": ip,
                    "kind": kind,
                    "locked_out": lockout.scope if lockout else None,
                }
            },
        )

    def authenticate_session(
        self, secret: str | None, *, now: datetime | None = None
    ) -> Principal | None:
        """The principal for a session cookie, or None if it's unknown, ended or expired."""
        if not secret or len(secret) > MAX_SECRET:
            return None
        now = now or utcnow()
        with self.db.read() as conn:
            row = conn.execute(
                select(sessions, users.c.username, users.c.role, users.c.disabled_at)
                .add_columns(
                    users.c.created_at.label("user_created_at"), users.c.password_changed_at
                )
                .join(users, users.c.user_id == sessions.c.user_id)
                .where(sessions.c.token_hash == tokens.digest(secret))
            ).one_or_none()
        if row is None or row.revoked_at is not None or row.disabled_at is not None:
            return None
        if now >= self._expires_at(row.created_at, row.last_seen):
            return None
        if now - row.last_seen >= LAST_SEEN_RESOLUTION:
            with self.db.write() as conn:
                conn.execute(
                    update(sessions)
                    .where(sessions.c.session_id == row.session_id, sessions.c.revoked_at.is_(None))
                    .values(last_seen=now)
                )
        user = User(
            row.user_id,
            row.username,
            row.role,
            row.user_created_at,
            row.password_changed_at,
            row.disabled_at,
        )
        return Principal(user, "session", session_id=row.session_id)

    def logout(self, secret: str, *, now: datetime | None = None) -> bool:
        now = now or utcnow()
        with self.db.write() as conn:
            row = conn.execute(
                select(sessions.c.session_id, sessions.c.user_id).where(
                    sessions.c.token_hash == tokens.digest(secret),
                    sessions.c.revoked_at.is_(None),
                )
            ).one_or_none()
            if row is None:
                return False
            return _revoke_session(
                conn, row.session_id, reason="logout", actor=f"user:{row.user_id}", now=now
            )

    # --- sessions --------------------------------------------------------------------

    def _expires_at(self, created_at: datetime, last_seen: datetime) -> datetime:
        return min(created_at + self.max_age, last_seen + self.idle)

    def list_sessions(
        self,
        user_id: int | None = None,
        *,
        include_ended: bool = False,
        now: datetime | None = None,
    ) -> list[SessionInfo]:
        """Newest first. Ended sessions are kept for a while (see `prune`)."""
        now = now or utcnow()
        query = select(sessions).order_by(sessions.c.session_id.desc())
        if user_id is not None:
            query = query.where(sessions.c.user_id == user_id)
        with self.db.read() as conn:
            found = [
                SessionInfo(
                    r.session_id,
                    r.user_id,
                    r.created_at,
                    r.last_seen,
                    r.reauth_at,
                    r.ip,
                    r.user_agent,
                    r.revoked_at,
                    r.revoked_reason,
                    self._expires_at(r.created_at, r.last_seen),
                )
                for r in conn.execute(query)
            ]
        return found if include_ended else [s for s in found if s.active(now)]

    def revoke_session(
        self, session_id: int, *, actor: str, reason: str = "revoked", now: datetime | None = None
    ) -> bool:
        with self.db.write() as conn:
            return _revoke_session(
                conn, session_id, reason=reason, actor=actor, now=now or utcnow()
            )

    def revoke_all_sessions(
        self, user_id: int, *, actor: str, reason: str = "revoked", now: datetime | None = None
    ) -> int:
        with self.db.write() as conn:
            return _revoke_sessions(conn, user_id, reason=reason, actor=actor, now=now or utcnow())

    # --- re-auth ---------------------------------------------------------------------

    def settings(self) -> AuthSettings:
        with self.db.read() as conn:
            return _load_settings(conn)

    def reauth(
        self, principal: Principal, password: str, *, ip: str, now: datetime | None = None
    ) -> None:
        """Check the password again; on success the session counts as recently
        authenticated. Raises `NotAllowed`, `LockedOut` or `LoginFailed`."""
        session_id = _require_session(principal)
        self._check_password(principal.user, password, ip=ip, kind="reauth", now=now)
        with self.db.write() as conn:
            conn.execute(
                update(sessions)
                .where(sessions.c.session_id == session_id)
                .values(reauth_at=now or utcnow())
            )

    def _check_password(
        self, user: User, password: str, *, ip: str, kind: AttemptKind, now: datetime | None
    ) -> None:
        now = now or utcnow()
        with self.db.read() as conn:
            lockout = limits.check(conn, self.config.login, username=user.username, ip=ip, now=now)
            row = _load_user(conn, user_id=user.user_id)
        if lockout is not None:
            raise LockedOut(lockout, now)
        if row is None or not passwords.verify_password(row.password_hash, password):
            self._failed(user.username, ip, kind, now)
            raise LoginFailed("wrong password")
        with self.db.write() as conn:
            limits.record(conn, username=user.username, ip=ip, kind=kind, success=True, now=now)

    def is_recent(self, principal: Principal, *, now: datetime | None = None) -> bool:
        """Whether a sensitive step would be allowed now without asking (doesn't use up a
        one-shot re-auth). Always False for tokens."""
        if not principal.is_session:
            return False
        now = now or utcnow()
        with self.db.read() as conn:
            current = _load_settings(conn)
            reauth_at = _reauth_at(conn, principal)
        return _recent(current, reauth_at, now)

    def require_recent_auth(self, principal: Principal, *, now: datetime | None = None) -> None:
        """Gate a sensitive step. Raises `NotAllowed` for tokens and `ReauthRequired` when
        the password must be entered again. With a window of 0 this uses up the check."""
        session_id = _require_session(principal)
        now = now or utcnow()
        with self.db.write() as conn:
            current = _load_settings(conn)
            if not current.reauth_enabled:
                return
            reauth_at = _reauth_at(conn, principal)
            if not _recent(current, reauth_at, now):
                raise ReauthRequired("enter your password again to continue")
            if current.reauth_minutes == 0:
                conn.execute(
                    update(sessions)
                    .where(sessions.c.session_id == session_id)
                    .values(reauth_at=None)
                )

    def update_settings(
        self, principal: Principal, changes: dict[str, Any], *, now: datetime | None = None
    ) -> AuthSettings:
        """Change login settings (needs a recent password check)."""
        self.require_recent_auth(principal, now=now)
        return self._store_settings(changes, actor=principal.actor, now=now)

    def set_settings_from_host(
        self, changes: dict[str, Any], *, actor: str = "cli", now: datetime | None = None
    ) -> AuthSettings:
        """Change login settings from the host (`steward user settings`), no session needed."""
        return self._store_settings(changes, actor=actor, now=now)

    def _store_settings(
        self, changes: dict[str, Any], *, actor: str, now: datetime | None
    ) -> AuthSettings:
        now = now or utcnow()
        with self.db.write() as conn:
            current = _load_settings(conn)
            try:
                updated = AuthSettings.model_validate({**current.model_dump(), **changes})
            except ValidationError as exc:
                raise AccountError(f"invalid login settings: {exc}") from exc
            for key in changes:
                settings.set_value(conn, f"auth.{key}", getattr(updated, key), actor=actor, now=now)
        if not updated.reauth_enabled:
            log.warning("re-auth turned off", extra={"fields": {"actor": actor}})
        return updated

    def change_password(
        self,
        principal: Principal,
        current_password: str,
        new_password: str,
        *,
        ip: str,
        now: datetime | None = None,
    ) -> int:
        """Change your own password (the current one counts as the re-auth). Ends every
        other session. Returns how many were ended."""
        session_id = _require_session(principal)
        now = now or utcnow()
        passwords.check(new_password, username=principal.user.username)
        self._check_password(principal.user, current_password, ip=ip, kind="reauth", now=now)
        password_hash = passwords.hash_password(new_password)
        with self.db.write() as conn:
            _set_password(conn, principal.user.user_id, password_hash, now)
            ended = _revoke_sessions(
                conn,
                principal.user.user_id,
                reason="password changed",
                actor=principal.actor,
                now=now,
                keep=session_id,
            )
            conn.execute(
                update(sessions).where(sessions.c.session_id == session_id).values(reauth_at=now)
            )
            events.emit(
                conn,
                "auth.password_changed",
                {"user_id": principal.user.user_id, "sessions_ended": ended, "reset": False},
                actor=principal.actor,
                now=now,
            )
        return ended

    # --- API tokens ------------------------------------------------------------------

    def create_token(
        self,
        principal: Principal,
        label: str,
        scopes: list[str] | tuple[str, ...],
        *,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> tuple[TokenInfo, str]:
        """Create an API token from a session (needs a recent password check). Returns
        the token's details and the secret, which is shown once and never stored."""
        self.require_recent_auth(principal, now=now)
        return self.issue_token(
            principal.user.user_id,
            label,
            scopes,
            expires_at=expires_at,
            actor=principal.actor,
            now=now,
        )

    def issue_token(
        self,
        user_id: int,
        label: str,
        scopes: list[str] | tuple[str, ...],
        *,
        actor: str,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> tuple[TokenInfo, str]:
        """Create an API token without the session checks (host CLI and `create_token`)."""
        label = label.strip()
        if not 1 <= len(label) <= MAX_LABEL:
            raise AccountError(f"a token label is 1-{MAX_LABEL} characters")
        unknown = sorted(set(scopes) - set(TOKEN_SCOPES))
        if unknown or not scopes:
            raise AccountError(
                f"token scopes must be some of {', '.join(TOKEN_SCOPES)}"
                + (f" (not {', '.join(unknown)})" if unknown else "")
            )
        chosen = [s for s in TOKEN_SCOPES if s in scopes]
        now = now or utcnow()
        if expires_at is not None and expires_at <= now:
            raise AccountError("a token's expiry must be in the future")
        secret = tokens.new_api_token()
        with self.db.write() as conn:
            if _load_user(conn, user_id=user_id) is None:
                raise AccountError("no such user")
            token_id = _insert_id(
                conn.execute(
                    api_tokens.insert().values(
                        secret_hash=tokens.digest(secret),
                        hint=tokens.hint(secret),
                        user_id=user_id,
                        label=label,
                        scopes=chosen,
                        expires_at=expires_at,
                        actor=actor,
                        created_at=now,
                    )
                )
            )
            events.emit(
                conn,
                "auth.token_created",
                {"token_id": token_id, "user_id": user_id, "label": label, "scopes": chosen},
                actor=actor,
                now=now,
            )
            row = conn.execute(select(api_tokens).where(api_tokens.c.token_id == token_id)).one()
        return _token(row), secret

    def authenticate_token(
        self, secret: str | None, *, ip: str | None = None, now: datetime | None = None
    ) -> Principal | None:
        """The principal for an API token (`Authorization: Bearer stw_…`), or None."""
        if not secret or len(secret) > MAX_SECRET or not tokens.looks_like_api_token(secret):
            return None
        now = now or utcnow()
        with self.db.read() as conn:
            row = conn.execute(
                select(api_tokens).where(api_tokens.c.secret_hash == tokens.digest(secret))
            ).one_or_none()
            user_row = _load_user(conn, user_id=row.user_id) if row is not None else None
        if row is None or user_row is None or user_row.disabled_at is not None:
            return None
        info = _token(row)
        if not info.active(now):
            return None
        if info.last_used is None or now - info.last_used >= LAST_SEEN_RESOLUTION:
            with self.db.write() as conn:
                conn.execute(
                    update(api_tokens)
                    .where(api_tokens.c.token_id == info.token_id)
                    .values(last_used=now, last_ip=ip)
                )
        return Principal(
            _user(user_row), "token", token_id=info.token_id, scopes=frozenset(info.scopes)
        )

    def list_tokens(self, user_id: int | None = None) -> list[TokenInfo]:
        query = select(api_tokens).order_by(api_tokens.c.token_id)
        if user_id is not None:
            query = query.where(api_tokens.c.user_id == user_id)
        with self.db.read() as conn:
            return [_token(r) for r in conn.execute(query)]

    def revoke_token(self, token_id: int, *, actor: str, now: datetime | None = None) -> bool:
        now = now or utcnow()
        with self.db.write() as conn:
            done = conn.execute(
                update(api_tokens)
                .where(api_tokens.c.token_id == token_id, api_tokens.c.revoked_at.is_(None))
                .values(revoked_at=now)
            ).rowcount
            if done:
                events.emit(
                    conn, "auth.token_revoked", {"token_id": token_id}, actor=actor, now=now
                )
        return bool(done)

    # --- housekeeping ----------------------------------------------------------------

    def prune(self, *, now: datetime | None = None) -> tuple[int, int]:
        """Forget old login attempts and long-ended sessions. Returns (attempts, sessions)."""
        now = now or utcnow()
        cutoff = now - timedelta(days=self.config.login.retention_days)
        with self.db.write() as conn:
            attempts = limits.prune(conn, older_than=cutoff)
            ended = conn.execute(
                sessions.delete().where(
                    or_(
                        sessions.c.revoked_at < cutoff,
                        sessions.c.created_at < cutoff - self.max_age,
                        sessions.c.last_seen < cutoff - self.idle,
                    )
                )
            ).rowcount
        return attempts, ended

    def failed_attempts(self, since: datetime) -> list[tuple[str, str, str, datetime]]:
        with self.db.read() as conn:
            return limits.failures_since(conn, since)

    def lockout_for(self, *, username: str, ip: str, now: datetime | None = None) -> Lockout | None:
        with self.db.read() as conn:
            return limits.check(
                conn, self.config.login, username=username, ip=ip, now=now or utcnow()
            )


# --- helpers -----------------------------------------------------------------------


def _require_session(principal: Principal) -> int:
    if not principal.is_session or principal.session_id is None:
        raise NotAllowed("only a logged-in session can do this, not an API token")
    return principal.session_id


def _recent(current: AuthSettings, reauth_at: datetime | None, now: datetime) -> bool:
    if not current.reauth_enabled:
        return True
    if reauth_at is None:
        return False
    window = (
        timedelta(minutes=current.reauth_minutes) if current.reauth_minutes else ONE_SHOT_REAUTH
    )
    return now - reauth_at < window


def _reauth_at(conn: Connection, principal: Principal) -> datetime | None:
    value = conn.execute(
        select(sessions.c.reauth_at).where(
            sessions.c.session_id == principal.session_id, sessions.c.revoked_at.is_(None)
        )
    ).scalar()
    return value if isinstance(value, datetime) else None


def _load_settings(conn: Connection) -> AuthSettings:
    stored = settings.get_all(conn, "auth")
    try:
        return AuthSettings.model_validate(stored)
    except ValidationError as exc:
        # Fail closed: a damaged setting must not quietly weaken re-auth.
        raise AuthError(f"stored login settings are invalid: {exc}") from exc


def _insert_id(result: Any) -> int:
    return int(result.inserted_primary_key[0])


def _owner_count(conn: Connection) -> int:
    return int(conn.execute(select(func.count()).where(users.c.role == "owner")).scalar_one())


def _load_user(
    conn: Connection, *, username: str | None = None, user_id: int | None = None
) -> Row[Any] | None:
    query = select(users)
    if user_id is not None:
        query = query.where(users.c.user_id == user_id)
    elif username is not None:
        query = query.where(users.c.username == username)
    else:
        raise ValueError("give a username or a user_id")
    return conn.execute(query).one_or_none()


def _user(row: Row[Any] | None) -> User:
    assert row is not None  # noqa: S101 -- callers check
    return User(
        row.user_id,
        row.username,
        row.role,
        row.created_at,
        row.password_changed_at,
        row.disabled_at,
    )


def _token(row: Row[Any]) -> TokenInfo:
    return TokenInfo(
        row.token_id,
        row.user_id,
        row.label,
        tuple(s for s in TOKEN_SCOPES if s in row.scopes),
        row.hint,
        row.created_at,
        row.expires_at,
        row.last_used,
        row.last_ip,
        row.revoked_at,
    )


def _set_password(conn: Connection, user_id: int, password_hash: str, now: datetime) -> None:
    conn.execute(
        update(users)
        .where(users.c.user_id == user_id)
        .values(password_hash=password_hash, password_changed_at=now, updated_at=now)
    )


def _revoke_session(
    conn: Connection, session_id: int, *, reason: str, actor: str, now: datetime
) -> bool:
    row = conn.execute(
        select(sessions.c.user_id).where(
            sessions.c.session_id == session_id, sessions.c.revoked_at.is_(None)
        )
    ).one_or_none()
    if row is None:
        return False
    conn.execute(
        update(sessions)
        .where(sessions.c.session_id == session_id)
        .values(revoked_at=now, revoked_reason=reason)
    )
    events.emit(
        conn,
        "auth.session_revoked",
        {"session_id": session_id, "user_id": row.user_id, "reason": reason},
        actor=actor,
        now=now,
    )
    return True


def _revoke_sessions(
    conn: Connection,
    user_id: int,
    *,
    reason: str,
    actor: str,
    now: datetime,
    keep: int | None = None,
) -> int:
    query = select(sessions.c.session_id).where(
        sessions.c.user_id == user_id, sessions.c.revoked_at.is_(None)
    )
    if keep is not None:
        query = query.where(sessions.c.session_id != keep)
    ids = list(conn.execute(query).scalars())
    for session_id in ids:
        _revoke_session(conn, session_id, reason=reason, actor=actor, now=now)
    return len(ids)


__all__ = [
    "TOKEN_SCOPES",
    "AccountError",
    "Auth",
    "AuthError",
    "AuthSettings",
    "LockedOut",
    "LoginFailed",
    "NewSession",
    "NotAllowed",
    "Principal",
    "ReauthRequired",
    "SessionInfo",
    "TokenInfo",
    "TokenScope",
    "User",
    "normalize_username",
]
