# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

import asyncio

import pytest

from steward.sdk import (
    Daily,
    Every,
    HealthResult,
    JobContext,
    ModuleConfig,
    RegistrationConflict,
    Registrations,
    Registry,
)


async def _ok() -> HealthResult:
    return HealthResult("ok", "fine")


def test_registrations_record_the_owning_module() -> None:
    regs = Registrations()
    Registry("a", None, regs).health_check("a.disk", _ok)
    Registry("b", None, regs).health_check("b.model", _ok)

    assert [(r.module, r.name) for r in regs] == [("a", "a.disk"), ("b", "b.model")]
    assert [r.name for r in regs.by_module("b")] == ["b.model"]
    assert set(regs.health_checks()) == {"a.disk", "b.model"}
    assert asyncio.run(regs.health_checks()["a.disk"]()) == HealthResult("ok", "fine")


def test_same_name_twice_conflicts() -> None:
    regs = Registrations()
    r = Registry("a", None, regs)
    r.health_check("a.disk", _ok)
    with pytest.raises(RegistrationConflict):
        r.health_check("a.disk", _ok)


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


def _job(ctx: JobContext) -> None:
    pass


def test_names_must_be_prefixed_with_the_module() -> None:
    r = Registry("tags", None, Registrations())
    r.job("tags.apply", _job, lane="llm-local")
    r.health_check("tags", _ok)
    with pytest.raises(ValueError, match="must start with 'tags.'"):
        r.job("apply", _job, lane="cpu")
    with pytest.raises(ValueError, match="must start with 'tags.'"):
        r.job("tagsx.apply", _job, lane="cpu")
    with pytest.raises(ValueError, match="dotted snake_case"):
        r.job("tags.Apply", _job, lane="cpu")


def test_unknown_lane_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown lane"):
        Registry("a", None, Registrations()).job("a.x", _job, lane="gpu")  # type: ignore[arg-type]


def test_typed_accessors() -> None:
    from datetime import time, timedelta
    from pathlib import Path

    regs = Registrations()
    r = Registry("a", None, regs)
    r.job("a.work", _job, lane="cpu")
    r.subscribe("file.discovered", lambda e: None, name="a.on_file")
    r.schedule("a.hourly", Every(timedelta(hours=1)), job="a.work", payload={"x": 1})
    r.schedule("a.nightly", Daily(time(3, 0)), job="a.work")
    r.migrations(Path(__file__).parent, database="index")

    assert regs.job_types()["a.work"].lane == "cpu"
    assert regs.subscriptions()["a.on_file"].event_type == "file.discovered"
    assert regs.schedules()["a.hourly"].payload == {"x": 1}
    assert str(regs.schedules()["a.nightly"].when) == "daily 03:00"
    assert regs.migrations("index") == {"a": Path(__file__).parent}
    assert regs.migrations("state") == {}


def test_subscribe_needs_a_dotted_event_type() -> None:
    with pytest.raises(ValueError, match="area.name"):
        Registry("a", None, Registrations()).subscribe("file", lambda e: None, name="a.x")


def test_migrations_directory_must_exist(tmp_path: object) -> None:
    from pathlib import Path

    with pytest.raises(ValueError, match="does not exist"):
        Registry("a", None, Registrations()).migrations(Path("/nonexistent/dir"))


def test_every_needs_at_least_a_minute() -> None:
    from datetime import timedelta

    with pytest.raises(ValueError, match="at least 1 minute"):
        Every(timedelta(seconds=30))


def test_ui_pages() -> None:
    from fastapi import APIRouter

    regs = Registrations()
    r = Registry("a", None, regs)
    router = APIRouter()
    r.ui_page("a.page", router, path="/ui/a-page", title=" A page ")
    page = regs.ui_pages()["a.page"]
    assert (page.path, page.title, page.nav, page.router) == ("/ui/a-page", "A page", True, router)
    for bad in ("/a", "/ui/", "/ui/A", "/ui/a/b", "/api/v1/x"):
        with pytest.raises(ValueError, match="must look like"):
            r.ui_page("a.bad", router, path=bad, title="x")
    with pytest.raises(ValueError, match="title"):
        r.ui_page("a.untitled", router, path="/ui/untitled", title=" ")
    with pytest.raises(ValueError, match="must start with 'a.'"):
        r.ui_page("b.page", router, path="/ui/b", title="B")
