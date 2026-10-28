# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The registry: how modules add things to steward.

Each module gets its own `Registry` handle during `register`; every handle writes into
one shared `Registrations` collection that the core reads. Names are global per
extension point, so two modules registering the same name is an error.

Extension points are added here as features need them, not ahead of time
(docs/adr/0003-modules-and-sdk-boundary.md).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TypeVar, cast

from steward.sdk.health import HealthCheck
from steward.sdk.module import ModuleConfig

C = TypeVar("C", bound=ModuleConfig)


class RegistrationConflict(Exception):
    """Two modules registered the same name at the same extension point."""


@dataclass(frozen=True, slots=True)
class Registration:
    point: str
    name: str
    module: str
    item: object


class Registrations:
    """Everything every loaded module registered, in registration order."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], Registration] = {}

    def add(self, entry: Registration) -> None:
        key = (entry.point, entry.name)
        existing = self._entries.get(key)
        if existing is not None:
            raise RegistrationConflict(
                f"{entry.point} {entry.name!r} registered by both "
                f"{existing.module!r} and {entry.module!r}"
            )
        self._entries[key] = entry

    def __iter__(self) -> Iterator[Registration]:
        return iter(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    def at(self, point: str) -> list[Registration]:
        return [e for e in self._entries.values() if e.point == point]

    def by_module(self, module: str) -> list[Registration]:
        return [e for e in self._entries.values() if e.module == module]

    def health_checks(self) -> dict[str, HealthCheck]:
        return {e.name: cast(HealthCheck, e.item) for e in self.at("health_check")}


class Registry:
    """The handle a module receives in `Module.register`."""

    def __init__(
        self, module: str, config: ModuleConfig | None, registrations: Registrations
    ) -> None:
        self._module = module
        self._config = config
        self._registrations = registrations

    @property
    def module(self) -> str:
        """The name of the module this handle belongs to."""
        return self._module

    @property
    def config(self) -> ModuleConfig | None:
        """The module's validated config (an instance of its `config_model`), if any."""
        return self._config

    def config_as(self, model: type[C]) -> C:
        """The module's config, typed as `model` (normally the module's `config_model`)."""
        if not isinstance(self._config, model):
            raise TypeError(f"module {self._module!r} config is not a {model.__name__}")
        return self._config

    def health_check(self, name: str, check: HealthCheck) -> None:
        """Register an async health check shown on the health page and `/health`."""
        self._add("health_check", name, check)

    def _add(self, point: str, name: str, item: object) -> None:
        if not name:
            raise ValueError(f"{point} name must not be empty")
        self._registrations.add(Registration(point, name, self._module, item))
