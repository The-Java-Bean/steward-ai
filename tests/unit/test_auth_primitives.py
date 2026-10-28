# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

import ipaddress

import pytest
from argon2 import PasswordHasher

from steward.core.auth import client_ip, passwords, tokens
from steward.core.auth.service import AccountError, AuthSettings, normalize_username

GOOD = "correct horse battery"


def test_hash_and_verify_round_trip() -> None:
    stored = passwords.hash_password(GOOD)
    assert stored.startswith("$argon2id$")
    assert passwords.verify_password(stored, GOOD)
    assert not passwords.verify_password(stored, GOOD + "!")
    assert not passwords.verify_password("not a hash", GOOD)
    assert not passwords.needs_rehash(stored)


def test_passwords_are_nfkc_normalized() -> None:
    stored = passwords.hash_password("ｆｕｌｌｗｉｄｔｈ ｐａｓｓ")  # noqa: RUF001
    assert passwords.verify_password(stored, "fullwidth pass")


def test_weaker_parameters_need_a_rehash() -> None:
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(GOOD)
    assert passwords.verify_password(weak, GOOD)
    assert passwords.needs_rehash(weak)


def test_overlong_passwords_are_never_verified() -> None:
    stored = passwords.hash_password(GOOD)
    assert not passwords.verify_password(stored, "x" * (passwords.MAX_LENGTH + 1))


@pytest.mark.parametrize(
    ("password", "problem"),
    [
        ("short pass", "at least 12"),
        ("abababababab", "different characters"),
        ("Password1234", "too common"),
        ("qwertyuiop123!!", "too common"),
        ("my name is alice ok", "username"),
        ("x" * 2000, "at most"),
    ],
)
def test_password_rules(password: str, problem: str) -> None:
    found = passwords.problems(password, username="alice")
    assert any(problem in p for p in found), found
    with pytest.raises(passwords.WeakPasswordError):
        passwords.check(password, username="alice")


def test_a_reasonable_password_passes() -> None:
    assert passwords.problems(GOOD, username="alice") == []
    assert len(passwords.common_passwords()) > 100


def test_usernames_are_normalized_and_checked() -> None:
    assert normalize_username("  Owner@Example ") == "owner@example"
    for bad in ("", "-x", "a b", "x" * 65, "ä"):
        with pytest.raises(AccountError):
            normalize_username(bad)


def test_secrets_and_csrf() -> None:
    a, b = tokens.new_session_secret(), tokens.new_session_secret()
    assert a != b
    assert len(a) >= 43
    api = tokens.new_api_token()
    assert tokens.looks_like_api_token(api)
    assert not tokens.looks_like_api_token(a)
    assert tokens.digest(api) == tokens.digest(api) != tokens.digest(a)
    assert tokens.hint(api) == api[-4:]
    csrf = tokens.csrf_token(a)
    assert tokens.check_csrf(a, csrf)
    assert not tokens.check_csrf(b, csrf)
    assert not tokens.check_csrf(a, None)
    assert not tokens.check_csrf(a, "")


PROXIES = (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("fd00::/8"))


@pytest.mark.parametrize(
    ("peer", "xff", "expected"),
    [
        ("203.0.113.5", [], "203.0.113.5"),
        # an untrusted peer can't choose its address
        ("203.0.113.5", ["198.51.100.1"], "203.0.113.5"),
        ("10.0.0.2", ["198.51.100.1"], "198.51.100.1"),
        # a client-supplied value left of the real one is ignored
        ("10.0.0.2", ["1.2.3.4, 198.51.100.1"], "198.51.100.1"),
        ("10.0.0.2", ["1.2.3.4", "198.51.100.1, 10.0.0.3"], "198.51.100.1"),
        ("10.0.0.2", ["garbage"], "10.0.0.2"),
        ("10.0.0.2", ["198.51.100.1:4711"], "198.51.100.1"),
        ("10.0.0.2", ["[2001:db8::1]:443"], "2001:db8::1"),
        ("::ffff:10.0.0.2", ["198.51.100.1"], "198.51.100.1"),
        ("fd00::1", ["10.0.0.9"], "10.0.0.9"),
        (None, ["198.51.100.1"], "unknown"),
        ("not-an-ip", [], "unknown"),
    ],
)
def test_client_ip(peer: str | None, xff: list[str], expected: str) -> None:
    assert client_ip(peer, xff, trusted=PROXIES) == expected


def test_client_ip_without_trusted_proxies_ignores_the_header() -> None:
    assert client_ip("10.0.0.2", ["198.51.100.1"]) == "10.0.0.2"


def test_auth_settings_bounds() -> None:
    assert AuthSettings() == AuthSettings(reauth_enabled=True, reauth_minutes=15)
    with pytest.raises(ValueError, match="reauth_minutes"):
        AuthSettings(reauth_minutes=-1)
