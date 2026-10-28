# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""A minimal steward-ai module. Copy this package to start a new one."""

from __future__ import annotations

from typing import ClassVar

from steward.sdk import HealthResult, ModuleConfig, ModuleKind, Registry


class HelloConfig(ModuleConfig):
    greeting: str = "hello"


class HelloModule:
    name: ClassVar[str] = "hello"
    version: ClassVar[str] = "0.1.0"
    kind: ClassVar[ModuleKind] = "feature"
    requires: ClassVar[tuple[str, ...]] = ()
    config_model: ClassVar[type[ModuleConfig] | None] = HelloConfig

    def register(self, r: Registry) -> None:
        greeting = r.config_as(HelloConfig).greeting

        async def check() -> HealthResult:
            return HealthResult("ok", greeting)

        r.health_check("hello", check)
