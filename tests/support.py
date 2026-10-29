# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Test helpers: fake entry points, throwaway modules, and runtimes in temp dirs."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from steward.core.config import Config, load_config
from steward.core.runtime import Runtime, build_runtime
from steward.sdk import HealthResult, ModuleConfig, Registry


@dataclass
class FakeEntryPoint:
    name: str
    target: object
    provider: str = "fake-dist"
    value: str = "fake:Module"
    dist: Any = field(init=False)

    def __post_init__(self) -> None:
        self.dist = SimpleNamespace(name=self.provider)

    def load(self) -> object:
        if isinstance(self.target, Exception):
            raise self.target
        return self.target


async def ok_check() -> HealthResult:
    return HealthResult("ok")


def make_module(
    name: str,
    *,
    requires: tuple[str, ...] = (),
    config_model: type[ModuleConfig] | None = None,
    checks: tuple[str, ...] = (),
    kind: str = "feature",
    register: Callable[[Registry], None] | None = None,
) -> type:
    """A module class. By default it registers `<name>.registered` (a health check)."""

    def _register(self: object, r: Registry) -> None:
        if register is not None:
            register(r)
            return
        r.health_check(f"{name}.registered", ok_check)
        for check in checks:
            r.health_check(check, ok_check)

    return type(
        f"{name.title()}Module",
        (),
        {
            "name": name,
            "version": "1.0.0",
            "kind": kind,
            "requires": requires,
            "config_model": config_model,
            "register": _register,
        },
    )


def eps(*classes: type, provider: str = "fake-dist") -> list[FakeEntryPoint]:
    return [FakeEntryPoint(cls.name, cls, provider) for cls in classes]  # type: ignore[attr-defined]


def write_config(directory: Path, **files: dict[str, Any]) -> Path:
    """Write `<name>.yaml` files (keyword `steward=...` -> steward.yaml) with config_version."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (directory / f"{name}.yaml").write_text(yaml.safe_dump({"config_version": 1, **content}))
    return directory


def make_runtime(
    tmp_path: Path,
    modules: Iterable[type] = (),
    *,
    upgrade: bool = True,
    **files: dict[str, Any],
) -> Runtime:
    """A runtime with its data dir under `tmp_path`; the given modules are enabled."""
    module_list = list(modules)
    steward_file = {"data_dir": str(tmp_path / "data"), **files.pop("steward", {})}
    modules_file = files.pop("modules", None) or {
        "modules": {m.name: {"enabled": True} for m in module_list}  # type: ignore[attr-defined]
    }
    config_dir = write_config(
        tmp_path / "config", steward=steward_file, modules=modules_file, **files
    )
    config: Config = load_config(config_dir)
    runtime = build_runtime(config, entry_points=eps(*module_list))
    if upgrade:
        runtime.upgrade_databases()
    return runtime


def use_fast_hashes(monkeypatch: Any) -> None:
    """Cheap argon2 parameters keep tests fast; the real ones are tested in unit tests."""
    from argon2 import PasswordHasher

    from steward.core.auth import passwords
    from steward.modules.shares import credentials

    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr(passwords, "_hasher", cheap)
    monkeypatch.setattr(credentials, "_hasher", cheap)
    passwords._dummy_hash.cache_clear()
