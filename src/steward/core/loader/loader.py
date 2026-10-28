# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Discover modules through entry points, validate them, and run their `register`.

Fails closed: an unknown module, an unmet dependency, invalid config or a registration
conflict raises `ModuleLoadError`, and startup stops.
"""

from __future__ import annotations

import heapq
import importlib.metadata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, get_args

from pydantic import ValidationError

from steward.sdk import (
    MODULE_NAME_PATTERN,
    Module,
    ModuleConfig,
    ModuleKind,
    RegistrationConflict,
    Registrations,
    Registry,
)

ENTRY_POINT_GROUP = "steward.modules"
BUILTIN_DISTRIBUTION = "steward-ai"
RESERVED_NAMES = frozenset({"core", "steward"})


class ModuleLoadError(Exception):
    """Base class: the configured set of modules can't be loaded."""


class UnknownModuleError(ModuleLoadError):
    """A module is enabled but no installed package provides it."""


class DuplicateModuleError(ModuleLoadError):
    """Two installed packages provide a module with the same name."""


class InvalidModuleError(ModuleLoadError):
    """An entry point doesn't name a class that satisfies the module contract."""


class MissingDependencyError(ModuleLoadError):
    """A module requires another module that isn't enabled."""


class DependencyCycleError(ModuleLoadError):
    """Modules require each other in a cycle."""


class ModuleConfigError(ModuleLoadError):
    """A module's config section doesn't validate."""


class RegistrationError(ModuleLoadError):
    """A module's `register` failed or conflicted with another module."""


class EntryPointLike(Protocol):
    """The parts of `importlib.metadata.EntryPoint` the loader uses."""

    @property
    def name(self) -> str: ...
    @property
    def value(self) -> str: ...
    def load(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class LoadedModule:
    module: Module
    provider: str | None
    """The installed distribution that provides the module, if known."""
    config: ModuleConfig | None


@dataclass(frozen=True, slots=True)
class LoadedModules:
    modules: tuple[LoadedModule, ...]
    """In registration order: every module comes after the modules it requires."""
    registrations: Registrations

    def names(self) -> list[str]:
        return [m.module.name for m in self.modules]


def discover(
    entry_points: Iterable[EntryPointLike] | None = None,
) -> dict[str, EntryPointLike]:
    """Return the installed modules by name, without importing any of them."""
    if entry_points is None:
        entry_points = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    found: dict[str, EntryPointLike] = {}
    for ep in entry_points:
        if not MODULE_NAME_PATTERN.match(ep.name):
            raise InvalidModuleError(
                f"module name {ep.name!r} ({_provider(ep)}) must be snake_case "
                f"matching {MODULE_NAME_PATTERN.pattern}"
            )
        if ep.name in RESERVED_NAMES:
            raise InvalidModuleError(f"module name {ep.name!r} ({_provider(ep)}) is reserved")
        if ep.name in found:
            raise DuplicateModuleError(
                f"module {ep.name!r} is provided by both "
                f"{_provider(found[ep.name])} and {_provider(ep)}"
            )
        found[ep.name] = ep
    return found


@dataclass(frozen=True, slots=True)
class ModuleChoice:
    """What the owner configured for one module (from `modules.yaml`)."""

    enabled: bool | None = None
    settings: Mapping[str, Any] | None = None


def resolve_enabled(
    choices: Mapping[str, ModuleChoice],
    *,
    entry_points: Iterable[EntryPointLike] | None = None,
) -> tuple[dict[str, Mapping[str, Any] | None], list[str]]:
    """Decide which installed modules are enabled. Returns (name -> settings, warnings).

    A module is on if the owner says so. Otherwise built-in feature modules are on and
    everything else is off: installing a package never turns a module on by itself.
    """
    available = discover(entry_points)
    enabled: dict[str, Mapping[str, Any] | None] = {}
    warnings: list[str] = []
    for name, choice in choices.items():
        if name not in available:
            if choice.enabled:
                raise UnknownModuleError(
                    f"modules.yaml enables {name!r}, which isn't installed; "
                    f"installed: {', '.join(sorted(available)) or 'none'}"
                )
            warnings.append(f"modules.yaml mentions {name!r}, which isn't installed")
    for name, ep in sorted(available.items()):
        choice = choices.get(name, ModuleChoice())
        on = choice.enabled
        if on is None:
            on = is_builtin(ep) and _kind(ep) == "feature"
        if on:
            enabled[name] = choice.settings
    return enabled, warnings


def is_builtin(ep: EntryPointLike) -> bool:
    return _provider(ep) == BUILTIN_DISTRIBUTION


def load_modules(
    enabled: Mapping[str, Mapping[str, Any] | None],
    *,
    entry_points: Iterable[EntryPointLike] | None = None,
    core: Callable[[Registry], None] | None = None,
) -> LoadedModules:
    """Load the `enabled` modules (name -> raw config section) and register them.

    `core`, if given, registers the core's own jobs, schedules and migrations first,
    under the owner name `core`, through the same registry the modules use.
    """
    available = discover(entry_points)

    unknown = sorted(set(enabled) - set(available))
    if unknown:
        raise UnknownModuleError(
            f"enabled but not installed: {', '.join(unknown)}; "
            f"installed: {', '.join(sorted(available)) or 'none'}"
        )

    instances = {name: _instantiate(available[name]) for name in sorted(enabled)}

    for name, module in instances.items():
        missing = [dep for dep in module.requires if dep not in instances]
        if missing:
            details = [
                f"{dep} ({'not enabled' if dep in available else 'not installed'})"
                for dep in missing
            ]
            raise MissingDependencyError(f"module {name!r} requires {', '.join(details)}")

    registrations = Registrations()
    if core is not None:
        core(Registry("core", None, registrations))
    loaded: list[LoadedModule] = []
    for name in _dependency_order(instances):
        module = instances[name]
        config = _validate_config(module, enabled[name])
        try:
            module.register(Registry(name, config, registrations))
        except RegistrationConflict as exc:
            raise RegistrationError(str(exc)) from exc
        except Exception as exc:
            raise RegistrationError(f"module {name!r} failed to register: {exc!r}") from exc
        loaded.append(LoadedModule(module, _provider(available[name]), config))

    _check_cross_references(registrations)
    return LoadedModules(tuple(loaded), registrations)


def _check_cross_references(registrations: Registrations) -> None:
    job_types = registrations.job_types()
    for reg in registrations.at("schedule"):
        job = registrations.schedules()[reg.name].job
        if job not in job_types:
            raise RegistrationError(
                f"schedule {reg.name!r} ({reg.module}) runs job {job!r}, which isn't registered"
            )


def _kind(ep: EntryPointLike) -> str | None:
    try:
        cls = ep.load()
    except Exception as exc:
        raise InvalidModuleError(
            f"module {ep.name!r} ({ep.value}) failed to import: {exc!r}"
        ) from exc
    return getattr(cls, "kind", None)


def _provider(ep: EntryPointLike) -> str | None:
    dist = getattr(ep, "dist", None)
    return getattr(dist, "name", None)


def _instantiate(ep: EntryPointLike) -> Module:
    where = f"module {ep.name!r} ({ep.value})"
    try:
        cls = ep.load()
    except Exception as exc:
        raise InvalidModuleError(f"{where} failed to import: {exc!r}") from exc
    if not isinstance(cls, type):
        raise InvalidModuleError(f"{where}: the entry point must name a class")
    try:
        module = cls()
    except Exception as exc:
        raise InvalidModuleError(f"{where} failed to initialize: {exc!r}") from exc
    if not isinstance(module, Module):
        raise InvalidModuleError(f"{where} doesn't implement the Module protocol")

    problems = []
    if module.name != ep.name:
        problems.append(f"name is {module.name!r} but the entry point is {ep.name!r}")
    if not isinstance(module.version, str) or not module.version:
        problems.append("version must be a non-empty string")
    if module.kind not in get_args(ModuleKind):
        problems.append(f"kind must be one of {get_args(ModuleKind)}, not {module.kind!r}")
    if not isinstance(module.requires, tuple) or not all(
        isinstance(dep, str) for dep in module.requires
    ):
        problems.append("requires must be a tuple of module names")
    elif module.name in module.requires:
        problems.append("a module can't require itself")
    cm = module.config_model
    if cm is not None and not (isinstance(cm, type) and issubclass(cm, ModuleConfig)):
        problems.append("config_model must be None or a subclass of steward.sdk.ModuleConfig")
    if problems:
        raise InvalidModuleError(f"{where}: " + "; ".join(problems))
    return module


def _validate_config(module: Module, raw: Mapping[str, Any] | None) -> ModuleConfig | None:
    raw = raw or {}
    if module.config_model is None:
        if raw:
            raise ModuleConfigError(
                f"module {module.name!r} takes no config, got keys: {', '.join(sorted(raw))}"
            )
        return None
    try:
        return module.config_model.model_validate(raw)
    except ValidationError as exc:
        raise ModuleConfigError(f"module {module.name!r} config is invalid:\n{exc}") from exc


def _dependency_order(modules: Mapping[str, Module]) -> list[str]:
    """Topological order (dependencies first), alphabetical among equals."""
    waiting_on = {name: set(m.requires) for name, m in modules.items()}
    dependents: dict[str, list[str]] = {name: [] for name in modules}
    for name, deps in waiting_on.items():
        for dep in deps:
            dependents[dep].append(name)

    ready = [name for name, deps in waiting_on.items() if not deps]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        name = heapq.heappop(ready)
        order.append(name)
        for dependent in dependents[name]:
            waiting_on[dependent].discard(name)
            if not waiting_on[dependent]:
                heapq.heappush(ready, dependent)

    if len(order) != len(modules):
        stuck = sorted(set(modules) - set(order))
        raise DependencyCycleError(f"modules require each other in a cycle: {', '.join(stuck)}")
    return order
