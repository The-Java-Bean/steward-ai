# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from steward.core.loader import (
    DependencyCycleError,
    DuplicateModuleError,
    InvalidModuleError,
    MissingDependencyError,
    ModuleConfigError,
    RegistrationError,
    UnknownModuleError,
    discover,
    load_modules,
)
from steward.sdk import HealthResult, ModuleConfig, ModuleKind, Registry


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


async def _ok() -> HealthResult:
    return HealthResult("ok")


def make_module(
    name: str,
    *,
    requires: tuple[str, ...] = (),
    config_model: type[ModuleConfig] | None = None,
    checks: tuple[str, ...] = (),
    kind: str = "feature",
) -> type:
    def register(self: object, r: Registry) -> None:
        r.health_check(f"{name}.registered", _ok)
        for check in checks:
            r.health_check(check, _ok)

    return type(
        f"{name.title()}Module",
        (),
        {
            "name": name,
            "version": "1.0.0",
            "kind": kind,
            "requires": requires,
            "config_model": config_model,
            "register": register,
        },
    )


def eps(*classes: type) -> list[FakeEntryPoint]:
    return [FakeEntryPoint(cls.name, cls) for cls in classes]  # type: ignore[attr-defined]


class GreetingConfig(ModuleConfig):
    greeting: str = "hi"
    times: int = 1


def test_loads_only_enabled_modules() -> None:
    loaded = load_modules({"a": None}, entry_points=eps(make_module("a"), make_module("b")))
    assert loaded.names() == ["a"]
    assert [r.name for r in loaded.registrations] == ["a.registered"]
    assert loaded.modules[0].provider == "fake-dist"


def test_nothing_enabled_loads_nothing() -> None:
    loaded = load_modules({}, entry_points=eps(make_module("a")))
    assert loaded.names() == []
    assert len(loaded.registrations) == 0


def test_disabled_modules_are_never_imported() -> None:
    broken = FakeEntryPoint("broken", ImportError("boom"))
    loaded = load_modules({"a": None}, entry_points=[*eps(make_module("a")), broken])
    assert loaded.names() == ["a"]


def test_registers_dependencies_first_then_alphabetically() -> None:
    modules = eps(
        make_module("c"),
        make_module("photos", requires=("tags",)),
        make_module("tags", requires=("b",)),
        make_module("b"),
        make_module("a"),
    )
    loaded = load_modules(dict.fromkeys(["a", "b", "c", "photos", "tags"]), entry_points=modules)
    assert loaded.names() == ["a", "b", "c", "tags", "photos"]


def test_unknown_module_fails() -> None:
    with pytest.raises(UnknownModuleError, match="not installed: nope"):
        load_modules({"nope": None}, entry_points=eps(make_module("a")))


def test_duplicate_module_names_fail() -> None:
    a = make_module("a")
    with pytest.raises(DuplicateModuleError, match="dist-one and dist-two"):
        discover([FakeEntryPoint("a", a, "dist-one"), FakeEntryPoint("a", a, "dist-two")])


@pytest.mark.parametrize("bad_name", ["Photos", "files-ui", "1st", "", "a.b"])
def test_module_names_must_be_snake_case(bad_name: str) -> None:
    with pytest.raises(InvalidModuleError, match="snake_case"):
        discover([FakeEntryPoint(bad_name, make_module("a"))])


def test_missing_dependency_fails_and_says_why() -> None:
    modules = eps(make_module("photos", requires=("tags", "exif")), make_module("tags"))
    expected = r"tags \(not enabled\), exif \(not installed\)"
    with pytest.raises(MissingDependencyError, match=expected):
        load_modules({"photos": None}, entry_points=modules)


def test_dependency_cycle_fails() -> None:
    modules = eps(make_module("a", requires=("b",)), make_module("b", requires=("a",)))
    with pytest.raises(DependencyCycleError, match="a, b"):
        load_modules({"a": None, "b": None}, entry_points=modules)


def test_import_error_is_a_load_error() -> None:
    with pytest.raises(InvalidModuleError, match="failed to import"):
        load_modules({"a": None}, entry_points=[FakeEntryPoint("a", ImportError("boom"))])


def test_entry_point_must_name_a_class() -> None:
    instance = make_module("a")()
    with pytest.raises(InvalidModuleError, match="must name a class"):
        load_modules({"a": None}, entry_points=[FakeEntryPoint("a", instance)])


def test_entry_point_name_must_match_module_name() -> None:
    with pytest.raises(InvalidModuleError, match="name is 'a' but the entry point is 'b'"):
        load_modules({"b": None}, entry_points=[FakeEntryPoint("b", make_module("a"))])


def test_missing_attributes_fail() -> None:
    class NotAModule:
        name: ClassVar[str] = "a"

    with pytest.raises(InvalidModuleError, match="Module protocol"):
        load_modules({"a": None}, entry_points=[FakeEntryPoint("a", NotAModule)])


def test_invalid_kind_fails() -> None:
    with pytest.raises(InvalidModuleError, match="kind must be one of"):
        load_modules({"a": None}, entry_points=eps(make_module("a", kind="plugin")))


def test_module_cannot_require_itself() -> None:
    with pytest.raises(InvalidModuleError, match="require itself"):
        load_modules({"a": None}, entry_points=eps(make_module("a", requires=("a",))))


def test_config_model_must_extend_module_config() -> None:
    from pydantic import BaseModel

    class Loose(BaseModel):
        x: int = 1

    module = make_module("a", config_model=Loose)  # type: ignore[arg-type]
    with pytest.raises(InvalidModuleError, match=r"subclass of steward\.sdk\.ModuleConfig"):
        load_modules({"a": None}, entry_points=eps(module))


def test_config_is_validated_and_passed_to_register() -> None:
    seen: list[GreetingConfig] = []

    class Greeter:
        name: ClassVar[str] = "greeter"
        version: ClassVar[str] = "1.0.0"
        kind: ClassVar[ModuleKind] = "feature"
        requires: ClassVar[tuple[str, ...]] = ()
        config_model: ClassVar[type[ModuleConfig] | None] = GreetingConfig

        def register(self, r: Registry) -> None:
            seen.append(r.config_as(GreetingConfig))

    loaded = load_modules({"greeter": {"times": "3"}}, entry_points=eps(Greeter))
    assert seen == [GreetingConfig(greeting="hi", times=3)]
    assert loaded.modules[0].config == seen[0]


def test_missing_config_section_uses_defaults() -> None:
    loaded = load_modules(
        {"a": None}, entry_points=eps(make_module("a", config_model=GreetingConfig))
    )
    assert loaded.modules[0].config == GreetingConfig()


def test_unknown_config_keys_fail() -> None:
    module = make_module("a", config_model=GreetingConfig)
    with pytest.raises(ModuleConfigError, match="greetng"):
        load_modules({"a": {"greetng": "typo"}}, entry_points=eps(module))


def test_invalid_config_values_fail() -> None:
    module = make_module("a", config_model=GreetingConfig)
    with pytest.raises(ModuleConfigError, match="times"):
        load_modules({"a": {"times": "many"}}, entry_points=eps(module))


def test_config_for_a_module_without_config_fails() -> None:
    with pytest.raises(ModuleConfigError, match="takes no config"):
        load_modules({"a": {"x": 1}}, entry_points=eps(make_module("a")))


def test_conflicting_registrations_fail() -> None:
    modules = eps(make_module("a", checks=("disk",)), make_module("b", checks=("disk",)))
    with pytest.raises(RegistrationError, match="'disk' registered by both 'a' and 'b'"):
        load_modules({"a": None, "b": None}, entry_points=modules)


def test_exception_in_register_is_a_load_error() -> None:
    class Exploding:
        name: ClassVar[str] = "boom"
        version: ClassVar[str] = "1.0.0"
        kind: ClassVar[ModuleKind] = "integration"
        requires: ClassVar[tuple[str, ...]] = ()
        config_model: ClassVar[type[ModuleConfig] | None] = None

        def register(self, r: Registry) -> None:
            raise RuntimeError("kaboom")

    with pytest.raises(RegistrationError, match=r"'boom' failed to register.*kaboom"):
        load_modules({"boom": None}, entry_points=eps(Exploding))
