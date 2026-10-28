# steward-ai

> **Status: pre-alpha.** The foundation is in place (the module system and the
> project tooling), but there is nothing usable yet: no web UI, no indexing, no chat.
> This README describes what steward-ai is being built to do. Sections marked
> *(planned)* aren't implemented.

steward-ai is a self-hosted AI steward for the files on your server. It indexes and
tags your files, answers questions about them with citations, and can reorganize,
edit and share them when you ask, but every destructive or outward-facing action the
AI wants to take waits for your approval, and folders you mark private never leave the
machines you trust.

It's built as a **framework**: a small core plus modules. It runs on its own, and works
*alongside* other self-hosted programs (photo managers, chat UIs, single sign-on, other
file managers) through optional integration modules, without requiring any of them.

---

## For humans

### What it does *(planned)*
- **File manager in the browser:** browse, preview, upload (large files, resumable),
  download, move, rename and delete (to a trash folder, with undo).
- **Search:** keyword search over names and metadata; with an AI model connected, full
  text, OCR of scans, photo captions and meaning-based search.
- **Tags:** automatic tagging from a vocabulary you control.
- **Chat with your files:** ask questions and get answers that cite the file and page.
  Use the built-in chat page, or any OpenAI-compatible chat client.
- **AI actions with approval:** ask it to file the inbox, rename scans, fix a note or
  share a folder. It proposes; you approve; everything can be undone.
- **Locked Folder:** choose folders that are hidden everywhere and only open after
  re-entering your password.
- **Share links:** password-protected links with optional expiry, and one page listing
  every active and past share.
- **Privacy rules in code:** private and locked content is only ever sent to model
  servers you've marked as local; secrets like key files and password vaults are never read.

### How it adapts to your folders
steward-ai doesn't assume any folder layout. During setup you map your folders to
**folder types** (inbox, documents, media, notes, private, trash, archive, …) or create
your own (for example "receipts", based on documents, with its own tags and fields).
Behavior comes from the type, never from the folder's name, and renaming a folder keeps
its type.

### Requirements *(planned)*
- A Linux server with Docker and Docker Compose.
- One or more folders to manage.
- Optional: an OpenAI-compatible model server (Ollama, llama.cpp, vLLM, LM Studio, or a
  cloud API). Without one, file management, keyword search, the Locked Folder and share
  links still work.
- Optional: a reverse proxy for HTTPS and public share links.

### Setup *(planned; not available yet)*
```sh
git clone <repository-url> steward && cd steward
cp deploy/compose.example.yaml compose.yaml
cp deploy/example.env .env                    # choose the folder to manage, data dir, port
docker compose up -d
docker compose logs steward-api | grep "setup token"
# open http://<your-server>:8420/setup, paste the token, follow the wizard
```
The setup wizard creates your owner account, adds your folders, suggests folder types,
asks you to choose a private folder, and optionally connects a model server.

### Optional integrations *(planned)*
Each is a module you can turn on or off: OpenAI-compatible chat clients (including
Open WebUI), Immich, other file managers (File Browser first), cloud models, MCP,
notifications, single sign-on through a reverse proxy, and more.

### Development
You need [uv](https://docs.astral.sh/uv/) and `make` (Docker for the image).

```sh
make sync      # create .venv with all dependencies (uv downloads Python 3.13 if needed)
make check     # lint, type-check, import contracts, tests, license checks (CI runs this)
make format    # auto-fix formatting
uv run steward modules   # list installed modules and what they register
make image     # build the Docker image as steward:dev
```

To write a module, see [docs/modules.md](docs/modules.md). Design decisions are
recorded in [docs/adr/](docs/adr/). Changes are listed in [CHANGELOG.md](CHANGELOG.md).

### License
Licensed under the [Apache License 2.0](LICENSE). Third-party licenses are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and included in the image under `/licenses/`.

---

## For Claude agents

Detailed orientation so you don't have to scan the repository. Keep this section
accurate whenever the code changes. Local, untracked files may also exist for you:
`CLAUDE.md` (rules) and `claude-handoff.md` (user instructions, current state, next
steps, log). Read them first if present. A maintainer-only design document
(`plan.md`, untracked) is the authoritative design if present.

### Current status
- **Phase 0 (foundation) is done.** What exists: the project tooling, `steward.sdk`
  (module contract + registry + health checks), the module loader, a minimal CLI, an
  example out-of-tree module, the Dockerfile and compose example, ADRs 0001–0006.
- **Next: Phase 1 (core)**: config files, the two databases and migrations, job queue,
  events, libraries and folder types, discovery, auth, setup wizard, privacy gateway,
  executor + journal, keyword search, backup/export, and the `api`/`worker` commands.
- Everything in "Architecture" beyond the module system is still the agreed design,
  not code.

### What exists, file by file
| Path | What it is |
|---|---|
| `pyproject.toml` | Project `steward-ai` (import package `steward`, CLI `steward`); deps `pydantic`, `typer`; dev group; uv workspace with `examples/hello-module`; ruff, mypy (strict), pytest and import-linter config |
| `uv.lock`, `.python-version` (3.13) | Locked dependencies; dev Python |
| `Makefile` | `sync`, `check` (= `lint typecheck imports test headers licenses`), `format`, `notices`, `image`, `clean` |
| `src/steward/__init__.py` | `__version__` from package metadata |
| `src/steward/sdk/module.py` | `Module` protocol (ClassVars `name`, `version`, `kind`, `requires`, `config_model`; method `register(r)`), `ModuleKind`, `ModuleConfig` (Pydantic, `extra="forbid"`, frozen), `MODULE_NAME_PATTERN` |
| `src/steward/sdk/registry.py` | `Registry` (per-module handle: `.module`, `.config`, `.config_as(Model)`, `.health_check(name, fn)`, private `_add(point, name, item)`), `Registrations` (shared store; `at(point)`, `by_module`, `health_checks()`), `Registration`, `RegistrationConflict` |
| `src/steward/sdk/health.py` | `HealthResult(status, detail)`, `HealthStatus` (`ok`/`warn`/`fail`), `HealthCheck` (async no-arg callable) |
| `src/steward/core/loader/loader.py` | `discover()` (entry points, no imports), `load_modules(enabled, entry_points=None)` → `LoadedModules(modules, registrations)`; error classes, all subclasses of `ModuleLoadError` |
| `src/steward/modules/` | Built-in modules go here (none yet) |
| `src/steward/cli.py` | Typer app: `steward version`, `steward modules` (loads every installed module with default config) |
| `examples/hello-module/` | Out-of-tree module `hello` (dist `steward-hello`): config `greeting`, registers health check `hello`. Dev dependency only; not in the image |
| `tests/unit/` | Loader (fake entry points) and registry tests |
| `tests/integration/test_module_discovery.py` | Phase 0 acceptance: real entry-point discovery of `hello`, CLI output |
| `scripts/check_headers.py` | Fails if a `.py` file doesn't start with the SPDX line |
| `deploy/Dockerfile` | Multi-stage, base images pinned by digest, `uv sync --locked --no-dev`, venv at `/opt/venv`, runs as 1000:1000, license texts in `/licenses/`, default `CMD steward --help` |
| `deploy/compose.example.yaml`, `deploy/example.env` | Generic two-service compose (api + read-only worker); `api`/`worker` commands don't exist until Phase 1 |
| `.github/workflows/ci.yml` | `make check` and an image build; actions pinned by commit SHA |
| `docs/adr/` | ADRs 0001–0006, index, template |
| `docs/modules.md` | Module authoring guide |
| `CHANGELOG.md`, `THIRD_PARTY_NOTICES.md` | Keep a Changelog; generated by `make notices` |

### Module system (implemented)
- Entry-point group `steward.modules`; entry-point name = module name; value =
  `package:Class`. The loader instantiates the class with no arguments.
- Module names are snake_case (`^[a-z][a-z0-9_]*$`): same string for entry point,
  package, config key and table prefix.
- `load_modules({name: raw_config_or_None})` fails closed with: `UnknownModuleError`,
  `DuplicateModuleError`, `InvalidModuleError` (import failure, not a class, contract
  mismatch, bad kind/requires/config_model, requires itself), `MissingDependencyError`,
  `DependencyCycleError`, `ModuleConfigError`, `RegistrationError` (conflict or
  exception in `register`). Disabled modules are never imported. Registration order is
  topological, alphabetical among equals.
- Adding an extension point: add the interface to `steward.sdk`, a typed method on
  `Registry` that calls `self._add("<point>", name, item)`, a typed accessor on
  `Registrations` if the core needs one, re-export it in `steward/sdk/__init__.py`,
  tests, and a row in `docs/modules.md`.

### Architecture (agreed design)
- **Two processes, one image:** `steward api` (web UI, login, `/api/v1`, public share
  links, agent loop, and the *executor lane*, the only code that writes files) and
  `steward worker` (watcher + reconciler, SQLite job queue, processing pipeline, event
  dispatch). The worker mounts libraries **read-only**.
- **Storage:** two SQLite databases (WAL). `index.db` is rebuildable (text, chunks,
  embeddings, captions, keyed by content hash); `state.db` is irreplaceable (accounts,
  folder types and mappings, files, tags, proposals, journal, shares, events, jobs) and
  exported to JSON nightly.
- **Core vs modules:** the core is config, DB/migrations, queue/scheduler, event outbox,
  libraries and folder types, discovery, the privacy gateway (including locked folders),
  auth, the executor and journal, agent shell, API/UI/CLI shells, setup wizard, module
  loader. Everything else is a module discovered through the `steward.modules`
  entry-point group: feature modules (`files_ui`, `shares`, `documents`, `search`,
  `fields`, `tags`, `photos`, `chat`, `files_ai`, `triage`, `tag_gen`, `dedupe`,
  `digest`) and integration modules (`openai_api`, `openwebui`, `immich`, `filebrowser`,
  later `remote_llm`, `mcp`, `notify`, `proxy_auth`, `totp`, `passkeys`).
- **Extension points** (interfaces in `steward.sdk`): `Stage`, `Extractor`,
  `ChatModel`/`EmbedModel`, `VectorStore`, `KeywordIndex`, `FolderType`, `Tool`,
  `ActionType`, `ApprovalChannel`, `ChatSurface`, `AuthProvider`, `Notifier`,
  `ShareProvider`, `LinkBuilder`, `ImageSearch`, `ExternalViewRenderer`, event
  subscribers, schedules, UI routers, setup steps, CLI sub-apps, health checks, exporters.
- **Pipeline:** a DAG of stages declaring `requires`/`produces` artifact kinds; stage
  version bumps re-queue only dependents. Stages: `hash`, `extract`, `ocr`, `chunk`,
  `embed`, `exif`, `caption`, `fields`, `apply_tags`.
- **Folder types:** settings `description`, `index` (full/metadata/none), `hash`
  (full/sampled), `stages`, `sensitivity`, `locked`, `writable`, `shareable`,
  `triage_target`, `default_tags`, `fields`; custom types `extend` built-ins; mappings
  inherit down the tree and follow folder renames. Roles: `trash`, `edit_backups`,
  default `inbox`, `digest_output`.
- **Privacy:** sensitivity levels `normal` < `local-only` < `excluded`. Every model call
  goes through `core.llm` gateway with a `provenance` list of file IDs; `local-only` to
  a `remote` endpoint raises `PrivacyViolation`; conversations get tainted. Locked
  folders are invisible to every chat surface, search outside the Locked Folder view,
  shares and integrations.
- **Actions:** every file change is a registered `ActionType` (`plan`, `render`,
  `apply` through `GuardedFS`, `inverse`). User UI actions run directly (journaled,
  undoable); AI actions are `auto` only when additive or exposure-reducing, otherwise
  proposals that a human approves via a POST from a logged-in session (re-auth for
  sensitive ones). No API token can approve.
- **Auth:** local owner account, argon2id password, server-side sessions, configurable
  re-auth window (can be disabled), scoped API tokens (`read`, `propose`), first-run
  setup token printed in the log.

### Conventions and rules
- Every `.py` file starts with `# SPDX-License-Identifier: Apache-2.0` then
  a `# Copyright <year> <holder>` line matching `NOTICE` (the SPDX line is checked by
  `make headers`).
- `make check` must pass before committing. mypy is strict everywhere; ruff rules
  `E F W I B UP SIM RUF PT S`, line length 100.
- Import contracts (import-linter, `pyproject.toml`): `steward.sdk` imports nothing else
  from steward; `steward.modules` and out-of-tree modules don't import `steward.core` or
  `steward.cli`; `steward.core` never imports modules. Planned when the code exists:
  only `core.llm` imports `openai`; `core.domain` imports nothing internal.
  Only `core.executor` writes library files.
- Runtime dependencies must pass the license allow-list (`ALLOWED_LICENSES` in the
  Makefile); run `make notices` after changing dependencies.
- Record changes in `CHANGELOG.md` under "Unreleased"; decisions with long-term weight
  get an ADR in `docs/adr/`.
- No code refers to a folder by name or position; read behavior from folder types.
- Module tables are prefixed `<module>_` with their own Alembic branch; irreplaceable
  module data registers an `Exporter`.
- Logs never contain file content.
- Tests never call real models by default (`-m live` opts in); a fake LLM adapter is used.
- No personal or deployment-specific details in tracked files; use placeholders.
- Update this README (both sections) whenever the code changes.

### Planned repository layout (target; see the table above for what exists)
```
src/steward/
  sdk/        public interfaces, Registry, events, testing/ (contract suites)
  core/       domain, config, db, queue, events, discovery, folders, llm, auth,
              executor, agent, setup, loader
  modules/    one package per module (stages, tools, actions, ui, cli, setup,
              prompts/, migrations/, tests/)
  api/  ui/  cli.py
tests/{unit,integration,privacy,executor,contract,e2e,fixtures}/
docs/{adr,runbooks,deploy}/   deploy/{Dockerfile,compose.example.yaml}
evals/   config/*.example.yaml
```

### Tech stack
Python 3.12+ (3.13 in the image and for development), uv, FastAPI, Pydantic v2, SQLAlchemy 2 Core + Alembic, SQLite (WAL) with
FTS5 and sqlite-vec, `openai` SDK against OpenAI-compatible endpoints, watchdog,
BLAKE3, pypdf + docling, ocrmypdf + Tesseract, exiftool, argon2-cffi, Jinja2 + htmx,
Typer, pytest + hypothesis, ruff, mypy, import-linter.

### Roadmap
0. ~~Foundation: repo skeleton, SDK `Module`/`Registry`, module loader, ADRs, license.~~ Done.
1. Core: config, DBs, queue, events, libraries/folder types, discovery, auth, setup
   wizard, privacy gateway, executor + journal, keyword search, backup/export.
2. File manager, Locked Folder, built-in shares.
3. Indexing and tagging (documents, search, fields, tags, photos).
4. Chat and AI actions (agent, built-in chat, proposals, triage, `openai_api`, `openwebui`).
5. Integrations (`immich`).
6. Background intelligence (`tag_gen`, `dedupe`, `digest`).
7. Later modules (`remote_llm`, file-manager integrations, `mcp`, `notify`, SSO, 2FA, …).

### Commands
```sh
make sync                       # uv sync --locked (dev deps + example module)
make check                      # lint typecheck imports test headers licenses
make format                     # ruff format + ruff check --fix
uv run pytest tests/unit -q     # a subset of tests
uv run steward modules          # list installed modules and registrations
make notices                    # regenerate THIRD_PARTY_NOTICES.md
make image && docker run --rm steward:dev steward version
```
uv may be installed at `~/.local/bin/uv`; make sure it's on `PATH`.
