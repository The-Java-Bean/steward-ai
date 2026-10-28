# 0003. Features are modules; `steward.sdk` is the only boundary

- Status: Accepted
- Date: 2026-10-27

## Context
steward-ai is meant to be a framework anyone can run and extend: it must work on its
own, and work *alongside* other programs (photo managers, chat UIs, single sign-on,
other file managers) without depending on any of them. An extension API only stays
honest if the main features are built on it too.

## Decision
- **Small core, everything else a module.** The core holds config, databases, the
  queue and scheduler, the event outbox, libraries and folder types, discovery, the
  privacy gateway, auth, the executor and journal, the agent shell, the API/UI/CLI
  shells, the setup wizard and the module loader. Features (file manager, shares,
  search, tags, chat…) and integrations with other programs are modules.
- **Discovery through entry points.** A module is a Python package that declares an
  entry point in the group `steward.modules`. The entry-point name is the module name;
  its value names the module class (`package:Class`). Built-in modules in
  `src/steward/modules/` register the same way as separately installed ones.
- **Module names are snake_case** (`^[a-z][a-z0-9_]*$`), so one name serves as the
  entry-point name, Python package, config key and `<module>_` table prefix.
- **The contract** (`steward.sdk.Module`): class attributes `name`, `version`,
  `kind` (`feature` or `integration`), `requires` (other module names) and
  `config_model` (`None` or a subclass of `steward.sdk.ModuleConfig`, which rejects
  unknown keys), and a `register(r: Registry)` method. The loader instantiates the
  class with no arguments and calls `register` once.
- **The loader fails closed.** Startup stops if an enabled module isn't installed, two
  packages provide the same name, a module doesn't satisfy the contract, a required
  module isn't enabled, modules require each other in a cycle, a config section doesn't
  validate, or two modules register the same name at the same extension point.
  Modules register in dependency order (alphabetical among equals), and disabled
  modules are never imported.
- **`steward.sdk` is the only import boundary.** Modules may import `steward.sdk`
  and their own package, nothing else from steward. The SDK imports nothing from the
  rest of steward. The core never imports a module directly. Enforced with
  import-linter contracts in `pyproject.toml`, run by `make check`.
- **Extension points are added when a feature needs one**, not speculatively. Each
  is a method on `Registry` and an interface in `steward.sdk`, and ships a shared
  contract test suite in `steward.sdk.testing`.
- The SDK is versioned with steward-ai. Before 1.0 it may change in any release with a
  CHANGELOG entry; from 1.0, breaking it needs a major version and an ADR.

## Consequences
- An out-of-tree module (`examples/hello-module/`) works exactly like a built-in one;
  it doubles as the loader's acceptance test and as a template.
- Modules are installed and locked like any dependency (`uv.lock`), so a deployment's
  module set is reproducible.
- Guarantees that live in core code paths modules are forced through (privacy,
  locked folders, approvals, the single writer) apply to every module automatically.
- When a feature can't be built without changing the core, that's a missing extension
  point: add the extension point first, then the feature.
- More indirection than a monolith: a feature touches its module, the SDK interface
  it implements, and the core service that consumes it.
