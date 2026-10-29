# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The registry: how modules add things to steward.

Each module gets its own `Registry` handle during `register`; every handle writes into
one shared `Registrations` collection that the core reads. Names are global per
extension point, so two modules registering the same name is an error, and every name
a module registers is `<module>` or starts with `<module>.`.

Extension points are added here as features need them, not ahead of time
(docs/adr/0003-modules-and-sdk-boundary.md).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast, get_args

from sqlalchemy import Connection

from steward.sdk.actions import ActionType
from steward.sdk.events import EventHandler
from steward.sdk.health import HealthCheck
from steward.sdk.jobs import LANES, WORKER_LANES, JobHandler, Lane
from steward.sdk.module import ModuleConfig
from steward.sdk.schedules import When
from steward.sdk.stages import ARTIFACT_PATTERN, AppliesTo, Stage, StageRunner, _always
from steward.sdk.ui import UI_PATH_PATTERN, UiPage
from steward.sdk.web import LinkBuilder

if TYPE_CHECKING:
    from fastapi import APIRouter

C = TypeVar("C", bound=ModuleConfig)

type Exporter = Callable[[Connection], Mapping[str, list[dict[str, Any]]]]
"""Reads the module's irreplaceable rows from `state.db`: `{table: [row, ...]}`."""

Database = Literal["state", "index"]
"""`state` holds what can't be rebuilt; `index` holds what can (docs/adr/0001)."""

CORE_ACTION_AREAS = ("file", "folder")
"""Action names the core registers without its `core.` prefix (`file.move`)."""

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")
EVENT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
ARTIFACT_KIND_PATTERN = re.compile(ARTIFACT_PATTERN)


class RegistrationConflict(Exception):
    """Two modules registered the same name at the same extension point."""


@dataclass(frozen=True, slots=True)
class Registration:
    point: str
    name: str
    module: str
    item: object


@dataclass(frozen=True, slots=True)
class JobType:
    handler: JobHandler = field(compare=False)
    lane: Lane


@dataclass(frozen=True, slots=True)
class Subscription:
    event_type: str
    handler: EventHandler = field(compare=False)


@dataclass(frozen=True, slots=True)
class Schedule:
    when: When
    job: str
    payload: Mapping[str, Any]


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

    def module_of(self, point: str, name: str) -> str:
        return self._entries[(point, name)].module

    def by_module(self, module: str) -> list[Registration]:
        return [e for e in self._entries.values() if e.module == module]

    def health_checks(self) -> dict[str, HealthCheck]:
        return {e.name: cast(HealthCheck, e.item) for e in self.at("health_check")}

    def job_types(self) -> dict[str, JobType]:
        return {e.name: cast(JobType, e.item) for e in self.at("job")}

    def subscriptions(self) -> dict[str, Subscription]:
        """Subscriber name -> subscription."""
        return {e.name: cast(Subscription, e.item) for e in self.at("subscriber")}

    def schedules(self) -> dict[str, Schedule]:
        return {e.name: cast(Schedule, e.item) for e in self.at("schedule")}

    def stages(self) -> dict[str, Stage]:
        return {e.name: cast(Stage, e.item) for e in self.at("stage")}

    def actions(self) -> dict[str, ActionType]:
        return {e.name: cast(ActionType, e.item) for e in self.at("action")}

    def exporters(self) -> dict[str, Exporter]:
        """Owner (`core` or a module name) -> exporter."""
        return {e.module: cast(Exporter, e.item) for e in self.at("exporter")}

    def ui_pages(self) -> dict[str, UiPage]:
        return {e.name: cast(UiPage, e.item) for e in self.at("ui_page")}

    def link_builders(self) -> dict[str, LinkBuilder]:
        return {e.name: cast(LinkBuilder, e.item) for e in self.at("link_builder")}

    def migrations(self, database: Database) -> dict[str, Path]:
        """Owner (`core` or a module name) -> directory of Alembic revision files."""
        return {e.module: cast(Path, e.item) for e in self.at(f"migrations:{database}")}


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

    def job(self, name: str, handler: JobHandler, *, lane: Lane) -> None:
        """Register a job type (`<module>.<name>`) and the lane its jobs run on.

        Jobs whose type isn't registered (for example because the module was disabled)
        stay queued until it is.
        """
        if lane not in LANES:
            raise ValueError(f"unknown lane {lane!r}; lanes are {', '.join(LANES)}")
        self._add("job", name, JobType(handler, lane))

    def subscribe(self, event_type: str, handler: EventHandler, *, name: str) -> None:
        """Call `handler` for every new event of `event_type`.

        `name` (`<module>.<name>`) identifies the subscriber; delivery resumes from where
        it left off when a disabled module is re-enabled.
        """
        if not EVENT_TYPE_PATTERN.match(event_type):
            raise ValueError(f"event type {event_type!r} must look like 'area.name'")
        self._add("subscriber", name, Subscription(event_type, handler))

    def schedule(
        self, name: str, when: When, *, job: str, payload: Mapping[str, Any] | None = None
    ) -> None:
        """Enqueue `job` (a registered job type) on a schedule. The owner can change the
        timing of any schedule, or turn it off, in `pipeline.yaml`."""
        self._add("schedule", name, Schedule(when, job, dict(payload or {})))

    def stage(
        self,
        name: str,
        run: StageRunner,
        *,
        version: int,
        produces: tuple[str, ...],
        requires: tuple[str, ...] = (),
        lane: Lane = "cpu",
        applies_to: AppliesTo = _always,
        default: bool = True,
        index: Literal["full", "metadata"] = "full",
    ) -> None:
        """Register a pipeline stage (`<module>.<name>`); see `steward.sdk.stages`.

        `requires` and `produces` are artifact kinds (`text`, `text:ocr`…). Core's
        `core.hash` stage produces `hash` and `mime`. Raise `version` whenever the
        output would change, so existing results are recomputed.
        """
        if version < 1:
            raise ValueError(f"stage {name!r}: version must be >= 1")
        if lane not in WORKER_LANES:
            raise ValueError(f"stage {name!r}: lane must be one of {', '.join(WORKER_LANES)}")
        if not produces:
            raise ValueError(f"stage {name!r} must produce at least one artifact kind")
        for kind in (*requires, *produces):
            if not ARTIFACT_KIND_PATTERN.match(kind):
                raise ValueError(f"stage {name!r}: bad artifact kind {kind!r}")
        if set(requires) & set(produces):
            raise ValueError(f"stage {name!r} requires an artifact kind it produces")
        if index not in ("full", "metadata"):
            raise ValueError(f"stage {name!r}: index must be 'full' or 'metadata'")
        item = Stage(
            name=name,
            version=version,
            produces=tuple(dict.fromkeys(produces)),
            run=run,
            requires=tuple(dict.fromkeys(requires)),
            lane=lane,
            applies_to=applies_to,
            default=default,
            index=index,
        )
        self._add("stage", name, item)

    def migrations(self, path: Path, *, database: Database = "state") -> None:
        """Register a directory of Alembic revision files for this module's tables.

        Tables must be named `<module>_*`. Each module's migrations have their own
        version table, so disabling a module never blocks anyone else's migrations.
        """
        if database not in get_args(Database):
            raise ValueError(f"database must be one of {get_args(Database)}")
        if not path.is_dir():
            raise ValueError(f"migrations directory {path} does not exist")
        self._add(f"migrations:{database}", self._module, path)

    def ui_page(
        self,
        name: str,
        router: APIRouter,
        *,
        path: str,
        title: str,
        nav: bool = True,
        templates: Path | None = None,
        static: Path | None = None,
    ) -> None:
        """Register a web UI page: `router` is mounted at `path` (`/ui/<slug>`), behind
        login and CSRF checks the module can't turn off (see `steward.sdk.ui`).
        `templates` and `static` are directories (see `UiPage`); a module's pages share
        one of each."""
        if not UI_PATH_PATTERN.match(path):
            raise ValueError(f"UI page path {path!r} must look like /ui/<lowercase-slug>")
        title = title.strip()
        if not title:
            raise ValueError("a UI page needs a title")
        for label, directory in (("templates", templates), ("static", static)):
            if directory is not None and not Path(directory).is_dir():
                raise ValueError(f"UI page {name!r}: {label} {directory} isn't a directory")
        self._add("ui_page", name, UiPage(path, router, title, nav, templates, static))

    def link_builder(self, name: str, builder: LinkBuilder) -> None:
        """Register how to link to a file or folder (`library:path`) in this module's
        pages; the core's pages (search results, for example) use the first one."""
        self._add("link_builder", name, builder)

    def action(self, action_type: ActionType) -> None:
        """Register an action type (see `steward.sdk.actions`). Its name is
        `<module>.<name>`; the core's own are `file.*` and `folder.*`."""
        name = action_type.name
        if action_type.ai_mode not in ("auto", "proposal"):
            raise ValueError(f"action {name!r}: ai_mode must be 'auto' or 'proposal'")
        if self._module == "core" and name.split(".", 1)[0] in CORE_ACTION_AREAS:
            self._registrations.add(Registration("action", name, "core", action_type))
            return
        self._add("action", name, action_type)

    def exporter(self, export: Exporter) -> None:
        """Register this module's exporter: it returns every irreplaceable row the module
        keeps (`{table: [row, ...]}`, JSON-compatible) for `steward export` and backups."""
        self._add("exporter", self._module, export)

    def _add(self, point: str, name: str, item: object) -> None:
        if not name:
            raise ValueError(f"{point} name must not be empty")
        if not NAME_PATTERN.match(name):
            raise ValueError(f"{point} name {name!r} must be lowercase dotted snake_case")
        if name != self._module and not name.startswith(self._module + "."):
            raise ValueError(f"{point} name {name!r} must start with '{self._module}.'")
        self._registrations.add(Registration(point, name, self._module, item))
