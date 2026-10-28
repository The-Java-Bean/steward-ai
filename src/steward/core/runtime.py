# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Assemble a running steward: config + databases + loaded modules."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property, partial
from pathlib import Path

from steward.core.auth import Auth
from steward.core.builtin import register_backup, register_core, register_deliveries
from steward.core.config import Config, ConfigError
from steward.core.db import Database, Databases, migrate
from steward.core.executor import Executor
from steward.core.llm import Gateway, check_llm_config
from steward.core.loader import (
    EntryPointLike,
    LoadedModules,
    ModuleChoice,
    load_modules,
    resolve_enabled,
)
from steward.core.privacy import PrivacyRules
from steward.core.scheduler import effective_schedules
from steward.core.stages import Planner, StageGraph, build_graph, register_stage_jobs
from steward.sdk import Database as DatabaseName
from steward.sdk import Registrations, Schedule


@dataclass(frozen=True)
class Runtime:
    config: Config
    dbs: Databases
    modules: LoadedModules
    schedules: dict[str, Schedule]
    """Registered schedules with `pipeline.yaml` overrides applied; `off` ones omitted."""
    warnings: tuple[str, ...]
    stages: StageGraph
    planner: Planner

    @property
    def registrations(self) -> Registrations:
        return self.modules.registrations

    @property
    def auth(self) -> Auth:
        return Auth(self.dbs.state, self.config.steward.auth)

    @cached_property
    def privacy(self) -> PrivacyRules:
        return PrivacyRules.from_config(self.config.privacy)

    @cached_property
    def executor(self) -> Executor:
        return Executor(
            self.dbs,
            self.registrations.actions(),
            max_attempts=self.config.pipeline.jobs.max_attempts,
        )

    @cached_property
    def gateway(self) -> Gateway:
        return Gateway(self.config.llm, self.dbs.state, self.privacy)

    def migration_owners(self, database: DatabaseName) -> dict[str, Path]:
        """Core first, then modules in dependency order."""
        found = self.registrations.migrations(database)
        order = ["core", *self.modules.names()]
        return {owner: found[owner] for owner in order if owner in found}

    def database(self, name: DatabaseName) -> Database:
        return self.dbs.state if name == "state" else self.dbs.index

    def upgrade_databases(self) -> dict[str, list[str]]:
        return {
            name: migrate.upgrade(self.database(name), self.migration_owners(name))
            for name in ("state", "index")
        }

    def ensure_databases_current(self) -> None:
        for name in ("state", "index"):
            migrate.ensure_current(self.database(name), self.migration_owners(name))

    def close(self) -> None:
        self.dbs.dispose()


def build_runtime(
    config: Config, *, entry_points: Iterable[EntryPointLike] | None = None
) -> Runtime:
    """Load modules per `modules.yaml` and wire up core. Raises ModuleLoadError (including
    StageGraphError) or ConfigError (fail closed). Doesn't touch the databases' contents."""
    entry_points = list(entry_points) if entry_points is not None else None
    try:
        check_llm_config(config.llm)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    choices = {
        name: ModuleChoice(entry.enabled, entry.settings)
        for name, entry in config.modules.modules.items()
    }
    enabled, warnings = resolve_enabled(choices, entry_points=entry_points)
    dbs = Databases.open(config.data_dir)
    modules = load_modules(
        enabled, entry_points=entry_points, core=partial(register_core, config=config, dbs=dbs)
    )
    register_deliveries(modules.registrations, dbs)
    built: list[Runtime] = []
    register_backup(modules.registrations, config, lambda: built[0])
    stages = build_graph(modules.registrations.stages())
    planner = register_stage_jobs(modules.registrations, dbs, stages, config)
    try:
        schedules = effective_schedules(
            modules.registrations.schedules(), config.pipeline.schedules
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    runtime = Runtime(config, dbs, modules, schedules, tuple(warnings), stages, planner)
    built.append(runtime)
    return runtime
