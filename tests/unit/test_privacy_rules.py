# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

import pytest

from steward.core.config import PrivacyConfig, PrivacyRule
from steward.core.privacy import PrivacyRules, matches


@pytest.mark.parametrize(
    ("relpath", "pattern", "expected"),
    [
        ("vault/db.kdbx", "*.kdbx", True),
        ("vault/DB.KDBX", "*.kdbx", True),
        ("a/b/id_rsa.pub", "id_rsa*", True),
        ("home/.ssh/config", ".ssh/**", True),
        ("home/.ssh", ".ssh/**", False),  # the folder itself, not its contents
        ("x/.env", ".env", True),
        ("x/.envrc", ".env", False),
        ("x/prod.env", "*.env", True),
        ("taxes/2025/return.pdf", "taxes/**", True),
        ("old/taxes/2025/return.pdf", "taxes/**", True),
        ("old/taxes/2025/return.pdf", "/taxes/**", False),
        ("taxes/2025/return.pdf", "/taxes/**", True),
        ("med/scan.pdf", "**/med/*.pdf", True),
        ("med/sub/scan.pdf", "**/med/*.pdf", False),
        ("med/sub/scan.pdf", "med/**/*.pdf", True),
        ("notes/secrets/plan.md", "secrets", True),  # a folder name covers its contents
        ("notes/secretsauce.md", "secrets", False),
        ("", "*.kdbx", False),
    ],
)
def test_matches(relpath: str, pattern: str, expected: bool) -> None:
    assert matches(relpath, pattern) is expected


def test_levels_only_go_up() -> None:
    rules = PrivacyRules.from_config(
        PrivacyConfig(
            config_version=1,
            extra_secret_patterns=("*.vault",),
            rules=(
                PrivacyRule(pattern="health/**", sensitivity="local-only"),
                PrivacyRule(pattern="health/keys/**", sensitivity="excluded"),
            ),
        )
    )
    assert rules.level_for("docs/a.txt") == "normal"
    assert rules.level_for("health/x.pdf") == "local-only"
    assert rules.level_for("health/keys/k.txt") == "excluded"
    assert rules.level_for("a/b.vault") == "excluded"
    assert rules.level_for("a/server.pem") == "excluded"  # defaults still apply
    replaced = PrivacyRules.from_config(PrivacyConfig(config_version=1, secret_patterns=()))
    assert replaced.level_for("a/server.pem") == "normal"


def test_rules_cannot_lower_sensitivity() -> None:
    with pytest.raises(ValueError, match="local-only"):
        PrivacyRule(pattern="*", sensitivity="normal")
