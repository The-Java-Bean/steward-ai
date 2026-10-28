# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from sqlalchemy import select, update
from support import make_runtime

from steward.core.auth import (
    AccountError,
    Auth,
    AuthError,
    LockedOut,
    LoginFailed,
    NotAllowed,
    Principal,
    ReauthRequired,
    WeakPasswordError,
)
from steward.core.auth import passwords as passwords_module
from steward.core.auth.jobs import check_health
from steward.core.db import utcnow
from steward.core.db.schema import events, login_attempts, sessions, settings_kv, users
from steward.core.runtime import Runtime

PASSWORD = "correct horse battery"
NEW_PASSWORD = "another long passphrase"
IP = "198.51.100.7"
NOW = utcnow().replace(microsecond=0)


@pytest.fixture(autouse=True)
def fast_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cheap argon2 parameters keep the suite fast; the real ones are tested in unit tests."""
    monkeypatch.setattr(
        passwords_module, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    )
    passwords_module._dummy_hash.cache_clear()


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    rt = make_runtime(
        tmp_path,
        steward={
            "auth": {
                "session_idle_hours": 2,
                "session_max_days": 1,
                "login": {
                    "max_failures_per_ip": 3,
                    "max_failures_per_account": 5,
                    "window_minutes": 15,
                    "lockout_minutes": 10,
                },
            }
        },
    )
    yield rt
    rt.close()


@pytest.fixture
def auth(runtime: Runtime) -> Auth:
    auth = runtime.auth
    auth.create_owner("Owner", PASSWORD, now=NOW)
    return auth


def event_types(runtime: Runtime) -> list[str]:
    with runtime.dbs.state.read() as conn:
        return list(conn.execute(select(events.c.type).order_by(events.c.event_id)).scalars())


def login(auth: Auth, *, at: timedelta = timedelta(0), ip: str = IP) -> tuple[str, Principal]:
    new = auth.login("owner", PASSWORD, ip=ip, user_agent="test", now=NOW + at)
    return new.secret, new.principal


# --- accounts -------------------------------------------------------------------------


def test_one_owner_only(runtime: Runtime) -> None:
    auth = runtime.auth
    assert not auth.has_owner()
    owner = auth.create_owner("Owner", PASSWORD)
    assert (owner.username, owner.role) == ("owner", "owner")
    assert auth.has_owner()
    with pytest.raises(AccountError, match="already exists"):
        auth.create_owner("second", PASSWORD)
    with runtime.dbs.state.read() as conn:
        stored = conn.execute(select(users.c.password_hash)).scalar_one()
    assert stored.startswith("$argon2id$")
    assert PASSWORD not in stored
    assert "auth.user_created" in event_types(runtime)


def test_owner_password_must_follow_the_rules(runtime: Runtime) -> None:
    with pytest.raises(WeakPasswordError, match="at least 12"):
        runtime.auth.create_owner("owner", "short")
    with pytest.raises(WeakPasswordError, match="username"):
        runtime.auth.create_owner("horse", PASSWORD)
    assert not runtime.auth.has_owner()


# --- login, sessions --------------------------------------------------------------------


def test_login_creates_a_session_with_a_hashed_secret(runtime: Runtime, auth: Auth) -> None:
    secret, principal = login(auth)
    assert principal.is_session
    assert principal.actor == "user:1"
    with runtime.dbs.state.read() as conn:
        row = conn.execute(select(sessions)).one()
    assert row.token_hash != secret
    assert secret not in row.token_hash
    assert (row.ip, row.user_agent) == (IP, "test")
    again = auth.authenticate_session(secret, now=NOW + timedelta(minutes=5))
    assert again is not None
    assert again.session_id == principal.session_id
    assert auth.authenticate_session("nope", now=NOW) is None
    assert auth.authenticate_session(None) is None
    assert "auth.login" in event_types(runtime)


def test_usernames_match_case_insensitively(auth: Auth) -> None:
    assert auth.login(" OWNER ", PASSWORD, ip=IP, now=NOW).principal.user.username == "owner"


def test_wrong_password_and_unknown_user_look_the_same(runtime: Runtime, auth: Auth) -> None:
    with pytest.raises(LoginFailed) as wrong:
        auth.login("owner", "wrong password!", ip=IP, now=NOW)
    with pytest.raises(LoginFailed) as unknown:
        auth.login("nobody", PASSWORD, ip="198.51.100.8", now=NOW)
    with pytest.raises(LoginFailed):
        auth.login("not a valid name", PASSWORD, ip="198.51.100.9", now=NOW)
    assert str(wrong.value) == str(unknown.value)
    assert event_types(runtime).count("auth.login_failed") == 3
    with runtime.dbs.state.read() as conn:
        attempts = conn.execute(select(login_attempts.c.username)).scalars().all()
    assert sorted(attempts) == ["nobody", "not a valid name", "owner"]


def test_sessions_expire_when_idle_and_at_max_age(auth: Auth) -> None:
    secret, _ = login(auth)
    # idle timeout 2h: a request after 1h59 keeps it alive...
    assert auth.authenticate_session(secret, now=NOW + timedelta(hours=1, minutes=59))
    assert auth.authenticate_session(secret, now=NOW + timedelta(hours=3, minutes=58))
    # ...but not past the 1-day maximum, however active
    for hour in range(4, 24, 1):
        assert auth.authenticate_session(secret, now=NOW + timedelta(hours=hour))
    assert auth.authenticate_session(secret, now=NOW + timedelta(days=1)) is None

    idle, _ = login(auth)
    assert auth.authenticate_session(idle, now=NOW + timedelta(hours=2)) is None


def test_last_seen_is_written_at_most_once_a_minute(runtime: Runtime, auth: Auth) -> None:
    secret, _ = login(auth)
    auth.authenticate_session(secret, now=NOW + timedelta(seconds=30))
    with runtime.dbs.state.read() as conn:
        assert conn.execute(select(sessions.c.last_seen)).scalar_one() == NOW
    auth.authenticate_session(secret, now=NOW + timedelta(seconds=90))
    with runtime.dbs.state.read() as conn:
        assert conn.execute(select(sessions.c.last_seen)).scalar_one() == NOW + timedelta(
            seconds=90
        )


def test_logout_and_revocation(runtime: Runtime, auth: Auth) -> None:
    first, _ = login(auth)
    second, p2 = login(auth)
    assert auth.logout(first, now=NOW)
    assert not auth.logout(first, now=NOW)
    assert auth.authenticate_session(first, now=NOW) is None
    assert [s.session_id for s in auth.list_sessions(now=NOW)] == [p2.session_id]
    assert len(auth.list_sessions(include_ended=True, now=NOW)) == 2
    assert p2.session_id is not None
    assert auth.revoke_session(p2.session_id, actor="user:1", now=NOW)
    assert auth.authenticate_session(second, now=NOW) is None
    assert event_types(runtime).count("auth.session_revoked") == 2


def test_disabled_users_cannot_log_in_or_use_sessions(runtime: Runtime, auth: Auth) -> None:
    secret, _ = login(auth)
    with runtime.dbs.state.write() as conn:
        conn.execute(update(users).values(disabled_at=NOW))
    assert auth.authenticate_session(secret, now=NOW) is None
    with pytest.raises(LoginFailed):
        login(auth)


def test_outdated_hashes_are_upgraded_on_login(
    runtime: Runtime, auth: Auth, monkeypatch: pytest.MonkeyPatch
) -> None:
    with runtime.dbs.state.read() as conn:
        before = conn.execute(select(users.c.password_hash)).scalar_one()
    monkeypatch.setattr(
        passwords_module, "_hasher", PasswordHasher(time_cost=2, memory_cost=16, parallelism=1)
    )
    login(auth)
    with runtime.dbs.state.read() as conn:
        after = conn.execute(select(users.c.password_hash)).scalar_one()
    assert after != before
    assert "t=2" in after
    login(auth)  # still works with the new hash


# --- rate limiting -------------------------------------------------------------------


def fail(auth: Auth, *, ip: str = IP, at: timedelta = timedelta(0), user: str = "owner") -> None:
    with pytest.raises(LoginFailed):
        auth.login(user, "wrong password!", ip=ip, now=NOW + at)


def test_an_address_is_locked_out_after_too_many_failures(runtime: Runtime, auth: Auth) -> None:
    for i in range(3):
        fail(auth, at=timedelta(seconds=i))
    # locked: even the right password is refused, without checking it or recording it
    with pytest.raises(LockedOut) as locked:
        login(auth, at=timedelta(minutes=1))
    assert locked.value.lockout.scope == "ip"
    assert 0 < locked.value.retry_after <= 10 * 60
    with runtime.dbs.state.read() as conn:
        assert len(conn.execute(select(login_attempts)).all()) == 3
    assert "auth.locked_out" in event_types(runtime)
    # another address is fine (3 < 5 per account)
    login(auth, ip="198.51.100.99", at=timedelta(minutes=1))
    # the lockout ends 10 minutes after the last failure
    login(auth, at=timedelta(minutes=10, seconds=3))


def test_a_success_resets_the_count(auth: Auth) -> None:
    fail(auth)
    fail(auth)
    login(auth, at=timedelta(seconds=1))
    fail(auth, at=timedelta(seconds=2))
    fail(auth, at=timedelta(seconds=3))
    login(auth, at=timedelta(seconds=4))


def test_failures_outside_the_window_do_not_count(auth: Auth) -> None:
    fail(auth)
    fail(auth)
    fail(auth, at=timedelta(minutes=16))
    login(auth, at=timedelta(minutes=16, seconds=1))


def test_an_account_is_locked_out_across_addresses(auth: Auth) -> None:
    for i in range(5):
        fail(auth, ip=f"203.0.113.{i}")
    with pytest.raises(LockedOut) as locked:
        login(auth, ip="203.0.113.200")
    assert locked.value.lockout.scope == "account"
    assert auth.unlock(username="owner") == 5
    login(auth, ip="203.0.113.200")


def test_failures_for_unknown_usernames_lock_the_address(auth: Auth) -> None:
    for name in ("admin", "root", "test"):
        fail(auth, user=name)
    with pytest.raises(LockedOut):
        login(auth)


# --- re-auth -------------------------------------------------------------------------


def test_login_counts_as_recent_for_the_window(auth: Auth) -> None:
    _, principal = login(auth)
    auth.require_recent_auth(principal, now=NOW + timedelta(minutes=14))
    assert auth.is_recent(principal, now=NOW + timedelta(minutes=14))
    with pytest.raises(ReauthRequired):
        auth.require_recent_auth(principal, now=NOW + timedelta(minutes=15))
    auth.reauth(principal, PASSWORD, ip=IP, now=NOW + timedelta(minutes=20))
    auth.require_recent_auth(principal, now=NOW + timedelta(minutes=30))


def test_wrong_reauth_password_counts_toward_lockout(runtime: Runtime, auth: Auth) -> None:
    _, principal = login(auth)
    for _ in range(3):
        with pytest.raises(LoginFailed):
            auth.reauth(principal, "wrong password!", ip=IP, now=NOW)
    with pytest.raises(LockedOut):
        auth.reauth(principal, PASSWORD, ip=IP, now=NOW)
    with runtime.dbs.state.read() as conn:
        kinds = set(conn.execute(select(login_attempts.c.kind)).scalars())
    assert kinds == {"login", "reauth"}


def test_window_zero_asks_every_time(auth: Auth) -> None:
    _, principal = login(auth)
    auth.update_settings(principal, {"reauth_minutes": 0}, now=NOW)
    auth.require_recent_auth(principal, now=NOW)  # the login itself allows one step
    with pytest.raises(ReauthRequired):
        auth.require_recent_auth(principal, now=NOW)
    auth.reauth(principal, PASSWORD, ip=IP, now=NOW + timedelta(minutes=1))
    assert auth.is_recent(principal, now=NOW + timedelta(minutes=1))
    auth.require_recent_auth(principal, now=NOW + timedelta(minutes=1))  # one step...
    with pytest.raises(ReauthRequired):
        auth.require_recent_auth(principal, now=NOW + timedelta(minutes=1))  # ...only
    auth.reauth(principal, PASSWORD, ip=IP, now=NOW + timedelta(minutes=2))
    with pytest.raises(ReauthRequired):  # and only shortly after the check
        auth.require_recent_auth(principal, now=NOW + timedelta(minutes=5))


def test_reauth_can_be_turned_off(runtime: Runtime, auth: Auth) -> None:
    _, principal = login(auth)
    auth.update_settings(principal, {"reauth_enabled": False}, now=NOW)
    auth.require_recent_auth(principal, now=NOW + timedelta(days=0, hours=1))
    assert not auth.settings().reauth_enabled
    assert "settings.changed" in event_types(runtime)
    health = check_health(auth)
    assert health.status == "warn"
    assert "re-auth is turned off" in health.detail


def test_changing_login_settings_needs_a_recent_check(auth: Auth) -> None:
    _, principal = login(auth)
    with pytest.raises(ReauthRequired):
        auth.update_settings(principal, {"reauth_minutes": 60}, now=NOW + timedelta(hours=1))
    with pytest.raises(AccountError, match="invalid login settings"):
        auth.update_settings(principal, {"reauth_minutes": -5}, now=NOW)
    with pytest.raises(AccountError, match="invalid login settings"):
        auth.update_settings(principal, {"unknown": 1}, now=NOW)
    assert auth.settings().reauth_minutes == 15


def test_damaged_stored_settings_fail_closed(runtime: Runtime, auth: Auth) -> None:
    _, principal = login(auth)
    with runtime.dbs.state.write() as conn:
        conn.execute(
            settings_kv.insert().values(
                key="auth.reauth_enabled", value="nah", actor="test", updated_at=NOW
            )
        )
    with pytest.raises(AuthError, match="invalid"):
        auth.require_recent_auth(principal, now=NOW)


def test_change_password_ends_other_sessions(runtime: Runtime, auth: Auth) -> None:
    other, _ = login(auth)
    current, principal = login(auth)
    with pytest.raises(LoginFailed):
        auth.change_password(principal, "wrong password!", NEW_PASSWORD, ip=IP, now=NOW)
    with pytest.raises(WeakPasswordError):
        auth.change_password(principal, PASSWORD, "short", ip=IP, now=NOW)
    assert auth.change_password(principal, PASSWORD, NEW_PASSWORD, ip=IP, now=NOW) == 1
    assert auth.authenticate_session(other, now=NOW) is None
    assert auth.authenticate_session(current, now=NOW) is not None
    with pytest.raises(LoginFailed):
        login(auth)
    auth.login("owner", NEW_PASSWORD, ip=IP, now=NOW)
    assert "auth.password_changed" in event_types(runtime)


def test_reset_password_from_the_host(auth: Auth) -> None:
    secret, _ = login(auth)
    for _ in range(3):
        fail(auth, ip=f"203.0.113.{_}")
    assert auth.reset_password("owner", NEW_PASSWORD, now=NOW) == 1
    assert auth.authenticate_session(secret, now=NOW) is None
    auth.login("owner", NEW_PASSWORD, ip=IP, now=NOW)
    with pytest.raises(AccountError):
        auth.reset_password("nobody", NEW_PASSWORD)


# --- API tokens ----------------------------------------------------------------------


def test_tokens_have_scopes_and_never_session_powers(runtime: Runtime, auth: Auth) -> None:
    _, session = login(auth)
    info, secret = auth.create_token(session, "phone", ["read"], now=NOW)
    assert secret.startswith("stw_")
    assert info.hint == secret[-4:]
    assert info.scopes == ("read",)
    token = auth.authenticate_token(secret, ip=IP, now=NOW)
    assert token is not None
    assert not token.is_session
    assert token.can("read")
    assert not token.can("propose")
    assert session.can("propose")
    for gated in (
        lambda: auth.require_recent_auth(token, now=NOW),
        lambda: auth.reauth(token, PASSWORD, ip=IP, now=NOW),
        lambda: auth.create_token(token, "more", ["read"], now=NOW),
        lambda: auth.update_settings(token, {"reauth_enabled": False}, now=NOW),
        lambda: auth.change_password(token, PASSWORD, NEW_PASSWORD, ip=IP, now=NOW),
    ):
        with pytest.raises(NotAllowed):
            gated()
    assert not auth.is_recent(token, now=NOW)
    assert "auth.token_created" in event_types(runtime)


def test_creating_a_token_needs_a_recent_check(auth: Auth) -> None:
    _, session = login(auth)
    with pytest.raises(ReauthRequired):
        auth.create_token(session, "late", ["read"], now=NOW + timedelta(hours=1))


@pytest.mark.parametrize(
    ("label", "scopes", "message"),
    [
        ("x", [], "scopes"),
        ("x", ["approve"], "not approve"),
        ("x", ["locked"], "not locked"),
        ("", ["read"], "label"),
        ("x" * 101, ["read"], "label"),
    ],
)
def test_token_validation(auth: Auth, label: str, scopes: list[str], message: str) -> None:
    with pytest.raises(AccountError, match=message):
        auth.issue_token(1, label, scopes, actor="cli")


def test_token_expiry_revocation_and_last_used(runtime: Runtime, auth: Auth) -> None:
    info, secret = auth.issue_token(
        1, "script", ["propose", "read"], actor="cli", expires_at=NOW + timedelta(days=1), now=NOW
    )
    assert info.scopes == ("read", "propose")
    assert auth.authenticate_token(secret, ip=IP, now=NOW + timedelta(hours=1))
    assert auth.list_tokens()[0].last_ip == IP
    assert auth.authenticate_token(secret, now=NOW + timedelta(days=1)) is None
    with pytest.raises(AccountError, match="future"):
        auth.issue_token(1, "old", ["read"], actor="cli", expires_at=NOW, now=NOW)

    _, other = auth.issue_token(1, "other", ["read"], actor="cli", now=NOW)
    assert auth.revoke_token(2, actor="cli", now=NOW)
    assert not auth.revoke_token(2, actor="cli", now=NOW)
    assert auth.authenticate_token(other, now=NOW) is None
    assert auth.authenticate_token("stw_" + "x" * 43, now=NOW) is None
    assert auth.authenticate_token("not a token", now=NOW) is None


# --- housekeeping and health -----------------------------------------------------------


def test_prune_forgets_old_attempts_and_ended_sessions(auth: Auth) -> None:
    login(auth)
    fail(auth)
    later = NOW + timedelta(days=31)
    _, recent = login(auth, at=timedelta(days=31))
    assert auth.prune(now=later) == (2, 1)
    assert [s.session_id for s in auth.list_sessions(include_ended=True, now=later)] == [
        recent.session_id
    ]


def test_health(runtime: Runtime) -> None:
    auth = runtime.auth
    assert check_health(auth).status == "warn"  # no owner yet
    auth.create_owner("owner", PASSWORD)
    healthy = check_health(auth)
    assert healthy.status == "ok"
    assert "0 failed" in healthy.detail
    for i in range(3):
        fail(auth, ip=f"203.0.113.{i}", at=timedelta(hours=24) - (utcnow() - NOW))
    status = check_health(auth)
    assert status.status == "warn"
    assert "3 failed" in status.detail
    check = runtime.registrations.health_checks()["core.auth"]
    assert asyncio.run(check()).status == "warn"


def test_prune_job_and_schedule_are_registered(runtime: Runtime) -> None:
    assert "core.auth.prune" in runtime.schedules
    assert "core.auth.prune" in runtime.registrations.job_types()


def test_runtime_settings_store(runtime: Runtime) -> None:
    from steward.core import settings

    with runtime.dbs.state.write() as conn:
        assert settings.set_value(conn, "auth.reauth_minutes", 5, actor="test")
        assert not settings.set_value(conn, "auth.reauth_minutes", 5, actor="test")
        assert settings.get_all(conn, "auth") == {"reauth_minutes": 5}
        assert settings.clear(conn, "auth.reauth_minutes", actor="test")
        assert not settings.clear(conn, "auth.reauth_minutes", actor="test")
        with pytest.raises(ValueError, match="owner.name"):
            settings.set_value(conn, "nodot", 1, actor="test")
    assert event_types(runtime).count("settings.changed") == 2
