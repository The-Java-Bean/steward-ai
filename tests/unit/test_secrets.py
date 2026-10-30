# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Finding credentials in text: what counts, and the everyday text that must not."""

from __future__ import annotations

import time

import pytest

from steward.core.config import PrivacyConfig
from steward.core.privacy import PrivacyRules, find_secret

# Put together here, so the repository holds nothing shaped like a live token.
GITHUB = "gh" + "p_" + "a1B2" * 9
AWS = "AKIA" + "ABCDEFGH" + "23456789"
SLACK = "xox" + "b-" + "1234567890-abcdefghij"
GOOGLE = "AI" + "za" + "a1B2c3D4e5" * 3 + "a1B2c"
MODEL = "sk-" + "ant-" + "a1B2c3D4" * 5
STEWARD = "stw_" + "a1B2c3D4e5F" * 3 + "a1B2c3D4e5"


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("-----BEGIN PRIVATE KEY-----\nabc", "private key"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----", "private key"),
        ("-----BEGIN RSA PRIVATE KEY-----", "private key"),
        ("-----BEGIN EC PRIVATE KEY-----", "private key"),
        ("-----BEGIN PGP PRIVATE KEY BLOCK-----", "private key"),
        (f"key is {AWS} here", "AWS key"),
        (f"git push https://{GITHUB}@example.com/x.git", "GitHub token"),
        (f"token {SLACK}", "Slack token"),
        (f"maps = {GOOGLE}", "Google API key"),
        (f"export KEY={MODEL}", "model API key"),
        (f"Authorization: {STEWARD}", "steward-ai token"),
        ("postgres://app:Tr0ub4dor3x@db.example.com/app", "password in a URL"),
        ("DB_PASSWORD=Tr0ub4dor3x", "password value"),
        ("password: Tr0ub4dor3x", "password value"),
        ('"password": "Tr0ub4dor3x"', "password value"),
        ("smtp_password = 'correct-horse-battery'", "password value"),
        ("passphrase: k9#mQ2!xLp", "passphrase value"),
        ("JWT_PRIVATE_KEY=9f8e7d6c5b4a39281706f5e4d3c2b1a0", "private_key value"),
        ("client_secret: 9f8e7d6c5b4a39281706", "secret value"),
        ("API_KEY=9f8e7d6c5b4a39281706", "api_key value"),
        ("auth_token := 9f8e7d6c5b4a39281706", "token value"),
    ],
)
def test_credentials_are_found(text: str, rule: str) -> None:
    assert find_secret(["nothing here", text]) == rule


@pytest.mark.parametrize(
    "text",
    [
        "",
        "We talked about the password policy at lunch.",
        "Enter your password: then press return.",
        "password: ${DB_PASSWORD}",
        "password: $DB_PASSWORD",
        "password: {{ vault_password }}",
        "password: <your password>",
        "password: changeme",
        "password=password123",
        "password: example-value",
        "password: ********",
        "password: xxxxxxxxxx",
        "password: 12345678",
        "password = self.password",
        "password = user.password",
        "password = getPassword()",
        "password: Authorization",
        "password=mypassword",
        "token: abc",
        "token = request.headers",
        "token_count: 123456789012345678",
        "secret: true",
        "secret = settings.SECRET_KEY",
        "api_key_env: MODEL_KEY",
        "api_key: your-api-key-goes-here",
        "accessToken: string",
        "sha256:9f8e7d6c5b4a39281706f5e4d3c2b1a0",
        "postgres://user:password@localhost/db",
        "postgres://user:${PASSWORD}@localhost/db",
        "http://localhost:8420/path@host",
        "-----BEGIN PUBLIC KEY-----",
        "-----BEGIN CERTIFICATE-----",
    ],
)
def test_everyday_text_isnt_a_finding(text: str) -> None:
    assert find_secret([text]) is None


def test_what_it_returns_never_holds_the_value() -> None:
    rule = find_secret(["password: Tr0ub4dor3x"])
    assert rule is not None
    assert "Tr0ub4dor3x" not in rule


def test_a_large_text_is_scanned_in_reasonable_time() -> None:
    text = "some ordinary line of text with words: password maybe, token: none\n" * 260_000
    started = time.monotonic()
    assert find_secret([text]) is None
    assert time.monotonic() - started < 30


def test_scanning_can_be_turned_off_or_skip_paths() -> None:
    config = PrivacyConfig(config_version=1, content_scan_ignore=("docs/**", "sample.env.txt"))
    rules = PrivacyRules.from_config(config)
    assert rules.scans_content("notes/a.txt")
    assert not rules.scans_content("x/docs/a.txt")
    assert not rules.scans_content("sample.env.txt")
    off = PrivacyRules.from_config(PrivacyConfig(config_version=1, content_scan=False))
    assert not off.scans_content("notes/a.txt")
