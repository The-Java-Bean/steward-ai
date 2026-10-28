# Writing a module

A steward-ai module is a Python package that declares an entry point in the
`steward.modules` group. Built-in modules and separately installed ones work the same
way. The design is in [ADR 0003](adr/0003-modules-and-sdk-boundary.md).

The smallest working example is [`examples/hello-module/`](../examples/hello-module/).
Copy it to start a new module.

## 1. The module class

```python
from typing import ClassVar

from steward.sdk import HealthResult, ModuleConfig, ModuleKind, Registry


class HelloConfig(ModuleConfig):  # unknown keys are rejected
    greeting: str = "hello"


class HelloModule:
    name: ClassVar[str] = "hello"  # snake_case; must equal the entry-point name
    version: ClassVar[str] = "0.1.0"  # the module's own version
    kind: ClassVar[ModuleKind] = "feature"  # or "integration"
    requires: ClassVar[tuple[str, ...]] = ()  # other modules that must be enabled
    config_model: ClassVar[type[ModuleConfig] | None] = HelloConfig

    def register(self, r: Registry) -> None:
        greeting = r.config_as(HelloConfig).greeting

        async def check() -> HealthResult:
            return HealthResult("ok", greeting)

        r.health_check("hello", check)
```

`register` runs once at startup, after every module in `requires`. It should only
register things; it must not do I/O or start background work.

## 2. The entry point

In the package's `pyproject.toml`:

```toml
[project.entry-points."steward.modules"]
hello = "steward_hello:HelloModule"
```

Install the package into the same environment (or image) as steward-ai, and
`steward modules` lists it.

## 3. Rules

- Import only `steward.sdk` and your own package. Nothing else from `steward`.
  (Built-in modules are checked by import-linter; out-of-tree modules should add the
  same contract to their own CI.)
- Never refer to a folder by name or position; read behavior from folder types.
- Write files only through an `ActionType` run by the executor (coming in Phase 1).
- Call models only through the SDK's gateway, with provenance (coming with the LLM layer).
- Store data in tables prefixed `<module>_`, and register an exporter for anything
  that can't be rebuilt (coming in Phase 1).
- Never put file contents in logs, events or notifications.

## Extension points available now

| `Registry` method | Registers |
|---|---|
| `health_check(name, check)` | an `async def` returning `HealthResult("ok" \| "warn" \| "fail", detail)` |

More are added as features need them. Names are global per extension point: two
modules registering the same name stops startup.

## Loader errors

Startup stops (fails closed) when an enabled module isn't installed, two packages
provide the same module name, a module doesn't match the contract, a required module
isn't enabled, modules require each other in a cycle, a config section doesn't
validate, or two modules register the same name.
