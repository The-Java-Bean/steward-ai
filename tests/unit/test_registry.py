# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

import asyncio

import pytest

from steward.sdk import (
    HealthResult,
    ModuleConfig,
    RegistrationConflict,
    Registrations,
    Registry,
)


async def _ok() -> HealthResult:
    return HealthResult("ok", "fine")


def test_registrations_record_the_owning_module() -> None:
    regs = Registrations()
    Registry("a", None, regs).health_check("disk", _ok)
    Registry("b", None, regs).health_check("model", _ok)

    assert [(r.module, r.name) for r in regs] == [("a", "disk"), ("b", "model")]
    assert [r.name for r in regs.by_module("b")] == ["model"]
    assert set(regs.health_checks()) == {"disk", "model"}
    assert asyncio.run(regs.health_checks()["disk"]()) == HealthResult("ok", "fine")


def test_same_name_twice_conflicts_even_within_one_module() -> None:
    regs = Registrations()
    r = Registry("a", None, regs)
    r.health_check("disk", _ok)
    with pytest.raises(RegistrationConflict):
        r.health_check("disk", _ok)


def test_empty_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        Registry("a", None, Registrations()).health_check("", _ok)


def test_config_as_checks_the_type() -> None:
    class Cfg(ModuleConfig):
        x: int = 1

    class Other(ModuleConfig):
        pass

    r = Registry("a", Cfg(), Registrations())
    assert r.config_as(Cfg).x == 1
    with pytest.raises(TypeError):
        r.config_as(Other)
    with pytest.raises(TypeError):
        Registry("a", None, Registrations()).config_as(Cfg)


def test_module_config_rejects_unknown_keys() -> None:
    from pydantic import ValidationError

    class Cfg(ModuleConfig):
        x: int = 1

    with pytest.raises(ValidationError):
        Cfg.model_validate({"y": 2})
