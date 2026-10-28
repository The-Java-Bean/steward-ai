# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

from typing import ClassVar

import pytest
from support import FakeEntryPoint, eps, make_module

from steward.core.loader import (
    DependencyCycleError,
    DuplicateModuleError,
    InvalidModuleError,
    MissingDependencyError,
    ModuleChoice,
    ModuleConfigError,
    RegistrationError,
    UnknownModuleError,
    discover,
    load_modules,
    resolve_enabled,
)
from steward.sdk import Daily, HealthResult, ModuleConfig, ModuleKind, Registry


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
    module = make_module("a", checks=("a.disk", "a.disk"))
    with pytest.raises(RegistrationError, match="'a.disk' registered by both 'a' and 'a'"):
        load_modules({"a": None}, entry_points=eps(module))


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


@pytest.mark.parametrize("reserved", ["core", "steward"])
def test_reserved_module_names_fail(reserved: str) -> None:
    with pytest.raises(InvalidModuleError, match="reserved"):
        discover([FakeEntryPoint(reserved, make_module(reserved))])


def test_core_registers_first_under_its_own_name() -> None:
    def core(r: Registry) -> None:
        r.health_check("core.db", _noop_check)

    loaded = load_modules({"a": None}, entry_points=eps(make_module("a")), core=core)
    assert [(r.module, r.name) for r in loaded.registrations] == [
        ("core", "core.db"),
        ("a", "a.registered"),
    ]


def test_schedule_must_name_a_registered_job() -> None:
    from datetime import time

    def register(r: Registry) -> None:
        r.schedule("a.nightly", Daily(time(3, 0)), job="a.missing")

    with pytest.raises(RegistrationError, match="isn't registered"):
        load_modules({"a": None}, entry_points=eps(make_module("a", register=register)))


class TestResolveEnabled:
    builtin_feature = make_module("files_ui")
    builtin_integration = make_module("immich", kind="integration")
    third_party = make_module("extra")

    def entry_points(self) -> list[FakeEntryPoint]:
        return [
            *eps(self.builtin_feature, self.builtin_integration, provider="steward-ai"),
            *eps(self.third_party, provider="someone-else"),
        ]

    def test_defaults(self) -> None:
        enabled, warnings = resolve_enabled({}, entry_points=self.entry_points())
        assert enabled == {"files_ui": None}
        assert warnings == []

    def test_owner_choices_win(self) -> None:
        choices = {
            "files_ui": ModuleChoice(enabled=False),
            "immich": ModuleChoice(enabled=True, settings={"url": "x"}),
            "extra": ModuleChoice(enabled=True),
        }
        enabled, _ = resolve_enabled(choices, entry_points=self.entry_points())
        assert enabled == {"immich": {"url": "x"}, "extra": None}

    def test_enabling_a_missing_module_fails(self) -> None:
        with pytest.raises(UnknownModuleError, match="isn't installed"):
            resolve_enabled({"nope": ModuleChoice(enabled=True)}, entry_points=self.entry_points())

    def test_mentioning_a_missing_disabled_module_warns(self) -> None:
        _, warnings = resolve_enabled(
            {"nope": ModuleChoice(enabled=False)}, entry_points=self.entry_points()
        )
        assert warnings == ["modules.yaml mentions 'nope', which isn't installed"]


async def _noop_check() -> HealthResult:
    return HealthResult("ok")
