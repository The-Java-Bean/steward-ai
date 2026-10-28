# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

import logging
from pathlib import Path

import pytest
from support import write_config

from steward.core.config import ConfigError, load_config


def test_empty_directory_gives_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    assert config.data_dir == Path("/data")
    assert config.steward.timezone == "UTC"
    assert config.modules.modules == {}
    assert config.pipeline.concurrency("cpu") == 2
    assert config.pipeline.concurrency("executor") == 1
    assert config.pipeline.jobs.lease_seconds == 300
    assert (config.steward.api.port, config.steward.api.secure_cookies) == (8420, "auto")
    assert config.steward.setup.library_dirs == (Path("/libraries"),)


def test_missing_directory_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(tmp_path / "nope")


def test_values_are_read(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        steward={"data_dir": "/srv/data", "timezone": "Europe/Berlin"},
        modules={"modules": {"hello": {"enabled": True, "settings": {"greeting": "hi"}}}},
        pipeline={"lanes": {"cpu": {"concurrency": 4}}, "schedules": {"core.prune_jobs": "off"}},
    )
    config = load_config(tmp_path)
    assert config.data_dir == Path("/srv/data")
    assert config.steward.tz.key == "Europe/Berlin"
    assert config.modules.modules["hello"].settings == {"greeting": "hi"}
    assert config.pipeline.concurrency("cpu") == 4
    assert config.pipeline.concurrency("io") == 1
    assert config.pipeline.schedules == {"core.prune_jobs": "off"}


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"steward": {"data_dri": "/x"}}, "data_dri"),
        ({"steward": {"timezone": "Mars/Base"}}, "unknown timezone"),
        ({"pipeline": {"lanes": {"gpu": {"concurrency": 1}}}}, "gpu"),
        ({"pipeline": {"lanes": {"executor": {"concurrency": 2}}}}, "single writer"),
        ({"pipeline": {"lanes": {"cpu": {"concurrency": 0}}}}, "concurrency"),
        ({"modules": {"modules": {"x": {"enabeld": True}}}}, "enabeld"),
    ],
)
def test_invalid_values_fail(
    tmp_path: Path, files: dict[str, dict[str, object]], message: str
) -> None:
    write_config(tmp_path, **files)
    with pytest.raises(ConfigError, match=message):
        load_config(tmp_path)


def test_config_version_is_required(tmp_path: Path) -> None:
    (tmp_path / "steward.yaml").write_text("data_dir: /x\n")
    with pytest.raises(ConfigError, match="config_version: 1"):
        load_config(tmp_path)


def test_future_config_version_fails(tmp_path: Path) -> None:
    (tmp_path / "steward.yaml").write_text("config_version: 2\n")
    with pytest.raises(ConfigError, match="isn't supported"):
        load_config(tmp_path)


@pytest.mark.parametrize("content", ["- a list\n", "key: [unclosed\n"])
def test_malformed_files_fail(tmp_path: Path, content: str) -> None:
    (tmp_path / "pipeline.yaml").write_text(content)
    with pytest.raises(ConfigError, match=r"pipeline\.yaml"):
        load_config(tmp_path)


def test_unknown_files_are_ignored_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "module.yaml").write_text("config_version: 1\n")
    (tmp_path / "steward.example.yaml").write_text("config_version: 1\n")
    with caplog.at_level(logging.WARNING):
        load_config(tmp_path)
    assert [r.fields["file"] for r in caplog.records] == ["module.yaml"]  # type: ignore[attr-defined]


def test_example_files_are_valid(tmp_path: Path) -> None:
    examples = sorted((Path(__file__).parents[2] / "config").glob("*.example.yaml"))
    assert {p.name for p in examples} == {
        "steward.example.yaml",
        "modules.example.yaml",
        "pipeline.example.yaml",
        "libraries.example.yaml",
        "folders.example.yaml",
        "privacy.example.yaml",
        "llm.example.yaml",
    }
    for example in examples:
        (tmp_path / example.name.replace(".example", "")).write_text(example.read_text())
    config = load_config(tmp_path)
    assert config.llm.roles["embed"].dims == 768
    assert "*.ovpn" in config.privacy.extra_secret_patterns
    assert config.steward.backup.keep == 7


def test_trusted_proxies_and_auth_settings(tmp_path: Path) -> None:
    directory = write_config(
        tmp_path / "c",
        steward={
            "trusted_proxies": ["10.0.0.0/8", "192.0.2.1", "fd00::/8"],
            "auth": {"session_idle_hours": 1, "login": {"lockout_minutes": 5}},
        },
    )
    config = load_config(directory)
    assert [str(n) for n in config.steward.trusted_proxies] == [
        "10.0.0.0/8",
        "192.0.2.1/32",
        "fd00::/8",
    ]
    assert config.steward.auth.session_idle_hours == 1
    assert config.steward.auth.login.lockout_minutes == 5
    bad = write_config(tmp_path / "d", steward={"trusted_proxies": ["not-an-address"]})
    with pytest.raises(ConfigError):
        load_config(bad)
    typo = write_config(tmp_path / "e", steward={"auth": {"login": {"max_failure": 3}}})
    with pytest.raises(ConfigError):
        load_config(typo)
