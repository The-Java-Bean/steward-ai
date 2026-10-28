# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

from pathlib import Path

import pytest
from argon2 import PasswordHasher
from support import write_config
from typer.testing import CliRunner, Result

from steward.cli import app
from steward.core.auth import Auth, LoginFailed
from steward.core.auth import passwords as passwords_module
from steward.core.config import load_config
from steward.core.db import Databases

PASSWORD = "correct horse battery"
NEW_PASSWORD = "another long passphrase"


@pytest.fixture(autouse=True)
def fast_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        passwords_module, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    )
    passwords_module._dummy_hash.cache_clear()


def run(config: Path, *args: str, input: str | None = None) -> Result:
    return CliRunner().invoke(app, ["--config-dir", str(config), *args], input=input)


@pytest.fixture
def config(tmp_path: Path) -> Path:
    directory = write_config(tmp_path / "config", steward={"data_dir": str(tmp_path / "data")})
    assert run(directory, "db", "upgrade").exit_code == 0
    return directory


def auth_for(config: Path) -> Auth:
    cfg = load_config(config)
    return Auth(Databases.open(cfg.data_dir).state, cfg.steward.auth)


def test_commands_before_setup(config: Path) -> None:
    assert "No accounts yet" in run(config, "user", "list").output
    result = run(config, "user", "reset-password", input=f"{NEW_PASSWORD}\n{NEW_PASSWORD}\n")
    assert result.exit_code == 1
    assert "setup wizard" in result.output


def test_reset_password_unlock_and_sessions(config: Path) -> None:
    auth = auth_for(config)
    auth.create_owner("owner", PASSWORD)
    auth.login("owner", PASSWORD, ip="198.51.100.7", user_agent="browser")
    assert "1  owner  owner" in run(config, "user", "list").output
    sessions = run(config, "user", "sessions").output
    assert "from 198.51.100.7" in sessions
    assert "browser" in sessions

    weak = run(config, "user", "reset-password", input="short\nshort\n")
    assert weak.exit_code == 1
    assert "at least 12" in weak.output
    reset = run(config, "user", "reset-password", input=f"{NEW_PASSWORD}\n{NEW_PASSWORD}\n")
    assert reset.exit_code == 0, reset.output
    assert "1 session(s) ended" in reset.output
    assert "No sessions." in run(config, "user", "sessions").output
    assert "ended" in run(config, "user", "sessions", "--all").output
    with pytest.raises(LoginFailed):
        auth.login("owner", PASSWORD, ip="198.51.100.7")
    assert "forgot 1 failed" in run(config, "user", "unlock").output
    auth.login("owner", NEW_PASSWORD, ip="198.51.100.7")
    assert "1 session(s) ended" in run(config, "user", "logout").output
    assert run(config, "user", "logout", "99").exit_code == 1


def test_settings(config: Path) -> None:
    auth_for(config).create_owner("owner", PASSWORD)
    assert "asks again after 15 minute(s)" in run(config, "user", "settings").output
    assert "every time" in run(config, "user", "settings", "--reauth-minutes", "0").output
    off = run(config, "user", "settings", "--no-reauth")
    assert "re-auth: OFF" in off.output
    assert "warning" in off.output
    bad = run(config, "user", "settings", "--reauth-minutes", "-1")
    assert bad.exit_code == 1
    assert "invalid login settings" in bad.output


def test_tokens(config: Path) -> None:
    auth = auth_for(config)
    auth.create_owner("owner", PASSWORD)
    created = run(config, "tokens", "create", "home automation", "--scope", "read")
    assert created.exit_code == 0, created.output
    secret = created.output.split(": ", 1)[1].split()[0]
    assert secret.startswith("stw_")
    assert auth.authenticate_token(secret) is not None
    listed = run(config, "tokens", "list").output
    assert "home automation  [read]" in listed
    assert secret not in listed
    assert run(config, "tokens", "create", "x", "--scope", "approve").exit_code == 1
    assert "revoked" in run(config, "tokens", "revoke", "1").output
    assert auth.authenticate_token(secret) is None
    assert "No API tokens." in run(config, "tokens", "list").output
    assert "revoked" in run(config, "tokens", "list", "--all").output
