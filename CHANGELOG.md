# Changelog

All notable changes to steward-ai are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Before 1.0, any release may change the SDK.

## [Unreleased]

### Added
- Project skeleton: `uv` project (`steward-ai`, package `steward`), ruff, mypy (strict),
  pytest, import-linter contracts, `make check`, CI workflow.
- `steward.sdk`: the `Module` protocol, `ModuleConfig`, `Registry` and
  `Registrations`, and the first extension point, health checks.
- Module loader (`steward.core.loader`): discovery through the `steward.modules`
  entry-point group, validation, dependency ordering, config validation; fails closed.
- `steward` CLI with `version` and `modules`.
- Example out-of-tree module (`examples/hello-module/`).
- Dockerfile (pinned base images, non-root, license texts in `/licenses/`),
  `deploy/compose.example.yaml`, `deploy/example.env`.
- Architecture decision records 0001–0006, module authoring guide.
- License checks: SPDX headers on every Python file, runtime dependency license
  allow-list, generated `THIRD_PARTY_NOTICES.md`.
