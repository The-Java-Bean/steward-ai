# steward-ai

> **Status: pre-alpha.** The foundation is in place (module system, configuration,
> databases, background jobs and events, file discovery, the processing pipeline that
> modules plug stages into, login, the setup wizard and a first web UI), but there is
> little to use yet: no file manager, no content indexing, no chat.
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

### Setup *(implemented; pre-alpha)*
```sh
git clone <repository-url> steward && cd steward
cp deploy/compose.example.yaml compose.yaml
cp deploy/example.env .env                    # choose the folder to manage, data dir, port
docker compose up -d
docker compose logs steward-api | grep "setup token"
# open http://<your-server>:8420/setup, paste the token, follow the wizard
```
The setup token proves you run the server; a new one is printed each time steward-ai
starts until setup is done. The wizard then:

1. creates your owner account (username and password);
2. adds your folders as **libraries**: it lists the folders mounted under `/libraries`
   and the ones in `libraries.yaml`, or you type a path (it can also import
   `libraries.yaml` and `folders.yaml` for you);
3. suggests a **folder type** for each top-level folder (from its name and the kinds of
   files inside); accept, change or skip each;
4. asks you to choose (or create) a **private folder**, optionally locked permanently,
   or to skip with a warning;
5. sets the **trash** and **edit-backup** folders (hidden `.steward/` folders by default).

If you stop halfway, logging in picks up where you left off. The wizard only adds
settings; it never changes a folder that already has a type. Connecting a model server
comes with the AI features. Everything can be changed later (for now with the
`steward` command line).

To try it without Docker: set `data_dir`, `api.port` and `setup.library_dirs` in a
`steward.yaml` outside the repository and run `steward api`.

### Optional integrations *(planned)*
Each is a module you can turn on or off: OpenAI-compatible chat clients (including
Open WebUI), Immich, other file managers (File Browser first), cloud models, MCP,
notifications, single sign-on through a reverse proxy, and more.

### Configuration *(partly implemented)*
Settings live in YAML files in a config directory (`/config` in the container, or
`--config-dir` / `STEWARD_CONFIG_DIR`). Every file is optional, and unknown keys are
errors. Annotated examples are in [`config/`](config/):

| File | Holds |
|---|---|
| `steward.yaml` | data directory, timezone, log level, trusted proxies, web server address and port (`api:`), Secure cookies, where setup looks for libraries (`setup:`), session lifetimes, login lockout limits |
| `modules.yaml` | which modules are on, and their settings |
| `pipeline.yaml` | background worker threads per lane, retries, retention, schedule timing, discovery (watching, temp-file patterns, disk throttling) |
| `libraries.yaml` | the folders steward-ai manages (imported with `steward folders import`) |
| `folders.yaml` | custom folder types, folder mappings, locks and roles (imported the same way) |
| `privacy.yaml` | files more sensitive than their folder: secret patterns (never read) and path/name rules (local-only or excluded) |
| `llm.yaml` | model servers (OpenAI-compatible, each marked `trust: local` or `remote`) and the roles that use them; none means no AI features |

`steward.yaml` also holds `backup:` (how many backups to keep, where, and how recent
one must be before a database migration).

`steward config check` validates them; `steward modules` shows installed modules.
Libraries and folder settings live in the database once imported (the setup wizard
and Settings will edit them there); importing again only adds and updates.

### Folders *(implemented)*
**Settings → Folders** (`/ui/settings/folders`) sets folder types, locks and unlocks
folders, and shows libraries and roles. Unlocking a folder or making it less private
asks for your password. The same from the command line; folders are written `library:path/inside/it` (`main:` is the library root).

```sh
steward folders import                  # apply libraries.yaml + folders.yaml
steward folders list                    # libraries, mappings, roles
steward folders show main:Documents/x   # effective settings, and why
steward folders map main:Photos media   # or: unmap
steward folders lock main:Taxes [--permanent]
steward folders unlock main:Taxes [--permanent]
steward folders role inbox main:Inbox   # trash, edit_backups, inbox, digest_output
steward folders move main:Old New       # a folder was renamed outside steward-ai
steward types list | show NAME | rename OLD NEW | delete NAME
```

A subfolder can never be less private than the folder it's in: if `Private` is
private and locked, so is everything inside it, whatever it's mapped to.

### Finding files *(implemented)*
`steward worker` scans every library when it starts and every 6 hours, and watches
for changes so new and edited files show up within seconds. To scan by hand:

```sh
steward scan                    # every library (or: steward scan main  /  main:Photos)
steward files list main:Photos  # known files; --missing for ones that disappeared
steward search invoice 2025     # file names and paths (also /ui/search)
```

**Search** finds files whose path contains every word (case and accents as typed are
ignored), names first, newest first, with filters for kind (document, image, ...),
library, folder, size and date. Locked folders, hidden folders and the trash are never
searched there.

steward-ai only reads your libraries. It recognizes edits, renames and moves (even
across libraries, and even when it wasn't running), so a file keeps its identity and a
renamed folder keeps its type, lock and roles. Deleted files are marked missing, never
forgotten. Temporary files (`*.part`, `~$*`, …), symlinks and folders of type
`ignored` are skipped. If a library's folder is suddenly empty (say, the disk isn't
mounted), the scan stops rather than marking everything missing; if it really is
empty, run `steward scan --allow-empty`.

For very large libraries, the watcher may need more inotify watches
(`sysctl fs.inotify.max_user_watches`); without them, changes are still found by the
6-hourly scan.

### Processing pipeline *(framework implemented; no content stages yet)*
After a file is found, its content goes through **stages** that modules provide (text
extraction, OCR, captions… arrive in later releases). Each unique content is processed
once, however many copies or moves it has, and in the order stages depend on each
other. Folder types decide which stages run where: `index: metadata` folders get only
name/metadata stages, and a type can add or remove stages
(`stages: {add: [ocr], remove: [caption]}`). When a module updates a stage, the worker
re-runs it, and whatever depends on it, in the background.

```sh
steward stages list   # stages in order, with done/skipped/failed counts
steward stages plan   # ask the worker to check every file for missing stages
```

### Login, settings and API tokens *(implemented)*
steward-ai has its own login: one owner account with a password (at least 12
characters; very common passwords are refused), created in the setup wizard. Log in at
`/login`; **Settings** (`/ui/settings`) changes your password, lists and logs out
sessions, sets the re-auth window, and creates and revokes API tokens.

- Sessions end after 12 hours without use or 7 days after login (`auth:` in
  `steward.yaml`).
- Sensitive steps (opening the Locked Folder, unlocking folders, creating API tokens,
  changing login settings) ask for your password again if you last entered it more
  than 15 minutes ago. You can change the window, set it to 0 (ask every time), or turn
  it off, in which case locked folders only need a normal login.
- After 10 wrong passwords from one address (or 50 for one username) within 15 minutes,
  logins are refused for 15 minutes.
- Behind a reverse proxy, list it under `trusted_proxies` so steward-ai sees the real
  client addresses.
- API tokens (`stw_…`) let scripts and other programs read (`read`) or suggest actions
  (`propose`), sent as `Authorization: Bearer stw_…`. No token can approve actions,
  open locked folders, change settings or open web pages.
- Every page and API route needs a login or a token; only `/login`, `/health` and the
  setup steps (before setup is done) are open. Forms are protected against cross-site
  requests. Behind HTTPS (directly or through a trusted proxy) cookies are marked
  `Secure`.

On the server (for recovery):

```sh
steward user reset-password          # forgot the password; ends all sessions
steward user unlock                  # lift a lockout early
steward user sessions [--all]        # list sessions;  steward user logout [ID]
steward user settings --reauth-minutes 30   # or --no-reauth / --reauth
steward tokens create "home automation" --scope read [--expires-days 90]
steward tokens list | revoke ID
```

### Privacy *(implemented)*
Every file is `normal`, `local-only` (never sent to a remote model) or `excluded`
(never read at all, not even to compute a checksum). Folder types set the level, and
`privacy.yaml` and per-file overrides can only raise it:

```sh
steward files sensitivity main:Notes/health.md local-only   # or excluded, clear
```

Key files, password databases, `.env` files and similar are excluded by default. All
model calls go through one gateway that enforces this; remote model servers aren't
supported yet and are refused at startup.

### Changes, undo, jobs and health *(implemented)*
steward-ai changes files only through its executor: never overwriting, never following
symlinks, deleting by moving to a trash folder (`.steward/trash` in each library unless
you chose one), and keeping a backup copy before editing. Files deleted from a locked
folder go to a locked part of the trash. Every change is listed in the **Journal**
(`/ui/journal`, `steward journal list`) and can be undone unless the files changed
since. **Jobs** (`/ui/jobs`) shows the background queue and retries jobs that gave up;
**Health** (`/ui/health`) shows checks, the last backup and recent failed logins.

### API *(implemented)*
`/api/v1` offers libraries, folders, files, search, the journal, jobs and health as
JSON, with its description at `/api/v1/openapi.json`. A token with the `read` scope
can read; running actions, undo and retrying jobs need a logged-in session with its
CSRF token in `X-CSRF-Token`. Locked folders never appear in API answers to tokens.

### Backups *(implemented)*
```sh
steward backup              # databases + state.json into <data_dir>/backups/<time>/
steward export -o state.json   # settings, files, journal... as JSON (no secrets)
```

A backup also runs every night at 03:30 (turn off `core.backup` in `pipeline.yaml` if
your backup tool runs `steward backup` first instead), and before any update that
changes the databases if the last one is more than a day old. Keep the backup folder
in your regular off-site backups. To restore, stop steward-ai and copy `state.db` and
`index.db` back into the data directory.

Locking and the login protect what steward-ai shows; they don't protect files from
someone with shell or disk access to the server.

### Development
You need [uv](https://docs.astral.sh/uv/) and `make` (Docker for the image).

```sh
make sync      # create .venv with all dependencies (uv downloads Python 3.13 if needed)
make check     # lint, type-check, import contracts, tests, license checks (CI runs this)
make format    # auto-fix formatting
make image     # build the Docker image as steward:dev
```

To run steward locally, keep its config and data outside the repository:

```sh
mkdir -p ../steward-dev && cp config/*.example.yaml ../steward-dev/   # then rename to *.yaml
export STEWARD_CONFIG_DIR=$PWD/../steward-dev   # set data_dir in steward.yaml too
uv run steward config check
uv run steward db upgrade      # create/migrate the databases (`steward api` also does it)
uv run steward api             # web UI on api.port (default 8420); prints a setup token
uv run steward worker          # background worker (Ctrl-C to stop)
uv run steward jobs list       # inspect jobs; `steward jobs retry <id>` for dead ones
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
- **Phase 0 done. Phase 1 (core): all 6 chunks done; next is Phase 2** (1: config, databases and migrations, job queue, events, scheduler,
  worker; 2: libraries, folder types, mappings, locks, roles, folder identity; 3:
  discovery: scanner, watcher, hashing, file identity; 3b: stage SDK, graph, planner,
  stage jobs, ADR 0011; 4: auth service, ADR 0012; 5: `steward api`, HTTP auth layer,
  setup wizard, settings page, `ui_page` extension point, ADR 0013; 6: privacy
  classification + model gateway (ADR 0015), executor/journal/undo + safety harness
  (ADR 0014), keyword search, `/api/v1`, `/ui/search|journal|jobs|health`,
  `/ui/settings/folders`, `steward backup|export` + pre-migration backups (ADR 0016)).
- Not yet: `steward setup --from`, `SetupStep` for modules, the model-endpoint setup
  step, remote model endpoints (`remote_llm` module), proposals/approval, htmx (no JS
  so far), libraries and roles editable in the UI (CLI only).

### What exists, file by file
| Path | What it is |
|---|---|
| `pyproject.toml` | Project `steward-ai` (import package `steward`, CLI `steward`); deps `pydantic`, `typer`, `sqlalchemy` 2.1, `alembic`, `pyyaml`, `blake3`, `watchdog`, `argon2-cffi`, `fastapi` (≥0.162; lazy router includes, see `iter_route_contexts`), `uvicorn`, `jinja2`, `python-multipart`; dev group (incl. `httpx2` for `TestClient`); uv workspace with `examples/hello-module`; ruff, mypy (strict, pydantic plugin), pytest (`pythonpath = tests`) and import-linter config |
| `uv.lock`, `.python-version` (3.13) | Locked dependencies; dev Python |
| `Makefile` | `sync`, `check` (= `lint typecheck imports test headers licenses`), `format`, `notices`, `image`, `clean` |
| `src/steward/__init__.py` | `__version__` from package metadata |
| `src/steward/sdk/module.py` | `Module` protocol (ClassVars `name`, `version`, `kind`, `requires`, `config_model`; method `register(r)`), `ModuleKind`, `ModuleConfig` (Pydantic, `extra="forbid"`, frozen), `MODULE_NAME_PATTERN` |
| `src/steward/sdk/registry.py` | `Registry` (per-module handle: `.module`, `.config`, `.config_as(Model)`, `.health_check(name, fn)`, private `_add(point, name, item)`), `Registrations` (shared store; `at(point)`, `by_module`, `health_checks()`), `Registration`, `RegistrationConflict` |
| `src/steward/sdk/health.py` | `HealthResult(status, detail)`, `HealthStatus` (`ok`/`warn`/`fail`), `HealthCheck` (async no-arg callable) |
| `src/steward/sdk/jobs.py` | `Lane` (`io`, `cpu`, `llm-local`, `llm-remote`, `events`, `executor`), `LANES`, `WORKER_LANES`, `JobContext(job_id, type, payload, attempt, renew_lease)`, `JobHandler`, `PermanentJobError` |
| `src/steward/sdk/events.py` | `Event(event_id, type, version, payload, actor, occurred_at)`, `EventHandler` |
| `src/steward/sdk/stages.py` | `Stage` (name, version, produces, run, requires, lane, applies_to, default, index; `short_name`), `StageContext` protocol (`stage`, `blob`, `file`, `folder`, `attempt`, `open()`, `index_read()`, `index_write()`, `renew_lease()`), `StageResult(status, reason, model, prompt_version)`, `BlobInfo`, `FileInfo`, `FolderInfo`, `FileChanged`, `StageRunner`, `AppliesTo`, literals `IndexPolicy`, `Sensitivity` (also used by `core.folders.types`), `StageStatus`, `ARTIFACT_PATTERN` |
| `src/steward/sdk/ui.py` | `UiPage(path, router, title, nav)`, `UI_PATH_PATTERN` (`/ui/<slug>`); `Registry.ui_page(name, router, *, path, title, nav=True)`, `Registrations.ui_pages()` (point `ui_page`) |
| `src/steward/sdk/schedules.py` | `Every(timedelta)` (≥1 min), `Daily(time)`, `When`; `str()` gives `every 6h` / `daily 03:00` |
| `src/steward/core/config/config.py` | `load_config(dir)` → `Config(steward, modules, pipeline)`; models `StewardConfig` (`data_dir`, `timezone`, `log_level`, `trusted_proxies`, `auth: AuthConfig` = `session_idle_hours`, `session_max_days`, `login: LoginLimits`; `api: ApiConfig` = `host` 0.0.0.0, `port` 8420, `secure_cookies` auto/always/never; `setup: SetupConfig` = `library_dirs` (`/libraries`)), `ModulesConfig` (`modules: {name: {enabled, settings}}`), `PipelineConfig` (`lanes`, `jobs`, `events`, `discovery: DiscoverySettings`, `schedules`); `ConfigError`; env `STEWARD_CONFIG_DIR` (default `/config`) |
| `src/steward/core/db/` | `engine.Database` (SQLite, WAL, FKs, busy timeout; `write()` = `BEGIN IMMEDIATE`, `read()` = deferred), `Databases.open(data_dir)` (`state.db`, `index.db`), `types.UTCDateTime` + `utcnow()`, `schema.py` (core tables as metadata), `migrate.py` (`upgrade`, `downgrade`, `status`, `ensure_current`, `MigrationError`), `alembic/` (shared env + template), `versions/{state,index}/` (core revisions) |
| `src/steward/core/queue/queue.py` | `enqueue`, `claim`, `renew`, `complete`, `fail`, `retry`, `prune_done`, `get`, `list_jobs`, `counts`, `Job`, `Backoff` |
| `src/steward/core/events.py` | `emit` (in caller's transaction), `dispatch` (per-subscriber cursors → delivery jobs), `load`, `prune`, `delivery_job_type` |
| `src/steward/core/scheduler.py` | `parse_when`, `effective_schedules` (applies `pipeline.yaml` overrides), `next_run`, `tick` |
| `src/steward/core/builtin.py` | Core's own registrations: jobs + daily schedules `core.prune_jobs` (04:00), `core.prune_events` (04:10), discovery (`register_discovery`), auth (`register_auth`), core actions, core migrations; `register_deliveries` (job type `core.deliver.<subscriber>` per subscriber); `register_backup` (after modules: job + daily 03:30 schedule `core.backup`, health check `core.backup`) |
| `src/steward/core/runtime.py` | `build_runtime(config, entry_points=None)` → `Runtime(config, dbs, modules, schedules, warnings, stages, planner)` (builds the stage graph after loading modules), property `auth` (a new `Auth` on `state.db`); `migration_owners(db)`, `upgrade_databases()`, `ensure_databases_current()`; cached `privacy` (`PrivacyRules`), `executor`, `gateway`; `build_runtime` raises `ConfigError` from `check_llm_config` |
| `src/steward/core/worker.py` | `Worker(runtime, lanes=, worker_id=)`: `run_one(lane)`, `housekeeping()` (dispatch + `stages.tick` + schedules), `drain()` (sync, for tests), `run(stop_event, housekeeping=True)` (threads per lane + housekeeping thread; `io` threads set idle IO priority); `new_worker_id(role)` |
| `src/steward/core/discovery/scanner.py` | `Scanner(dbs, settings, actor=, renew=, allow_empty_root=)`.`scan(library_id, paths)` → `ScanReport` (counts); walk → folders (inode rename detection → `store.move_folder`) → files (same path / inode / content-hash identity) → missing; `ScanError` |
| `src/steward/core/discovery/hashing.py` | `hash_file(path, "full"|"sampled", throttle=)` → `HashResult(content_hash, hash_kind, size, mime, kind)`; `detect_mime` (extension, then magic numbers), `kind_of`; `Throttle`; `HASH_STAGE`/`HASH_STAGE_VERSION` |
| `src/steward/core/discovery/watcher.py` | `Watcher(dbs, settings)`: watchdog observer per library root, `path_changed` → debounced pending paths, `flush()` → `core.scan` jobs (priority 10, ≤500 paths each), `refresh()` follows `libraries`, `run(stop)` |
| `src/steward/core/discovery/jobs.py` | `register_discovery`: jobs `core.reconcile` (queue a full `core.scan` per library, dedupe `scan:<lib>`) and `core.scan` (payload `library_id`, `paths`), schedule `core.reconcile` every 6h, subscribers `core.scan_added_library` (`library.added`) and `core.scan_moved_library` (`library.updated`) that queue a full scan |
| `src/steward/core/discovery/filters.py`, `ioprio.py` | `is_ignored(relpath, patterns)` (name globs; patterns with `/` match path tails); `set_idle_io_priority()` (ctypes `ioprio_set`, per thread) |
| `src/steward/core/stages/graph.py` | `build_graph(stages)` → `StageGraph(stages in topo order, producers)`: `root` (`core.hash`), `upstream(name)`, `downstream[name]`, `matching(names)` (full or short names), `signature`; `StageGraphError(ModuleLoadError)` |
| `src/steward/core/stages/planner.py` | `Planner(dbs, graph, max_attempts=)`: `plan_hashes`, `plan_files`, `plan_all(renew=)`, `ready`, `enqueue`, `present_files`; `Eligibility(graph, policy)` (folder settings cache, `enabled_stages(file)`, `candidates(stage, blob, files)`); `run_status(graph, runs)` → (current, usable); `RunRecord`, `Candidate`, `stage_job_type` (`core.stage.<name>`) |
| `src/steward/core/stages/jobs.py` | `register_hash_stage` (declares `core.hash`), `register_stage_jobs` (per-stage jobs, `core.stages.plan`, `core.stages.full`, daily 04:20 schedule; returns the `Planner`), `tick` (housekeeping trigger), `enqueue_full_pass`, `full_pass_needed`, the stage job handler (`_stage_handler`, `_record`) |
| `src/steward/core/stages/context.py` | `Context`: the `StageContext` implementation (`O_NOFOLLOW` open + size/mtime check, `unchanged()`) |
| `src/steward/core/auth/service.py` | `Auth(db, AuthConfig)`: `has_owner`, `create_owner`, `get_user`, `list_users`, `reset_password`, `unlock`; `login` → `NewSession(secret, principal)`; `authenticate_session`, `logout`, `list_sessions`, `revoke_session`, `revoke_all_sessions`; `settings`, `reauth`, `is_recent`, `require_recent_auth`, `update_settings` (needs recent auth), `set_settings_from_host`, `change_password`; `create_token` (session + recent auth), `issue_token` (no checks; CLI), `authenticate_token`, `list_tokens`, `revoke_token`; `prune`, `failed_attempts`, `lockout_for`. Value objects `User`, `Principal` (`via` session/token, `can(scope)`, `actor`), `SessionInfo`, `TokenInfo`, `AuthSettings`; errors `AuthError` > `LoginFailed`, `LockedOut` (`retry_after`), `ReauthRequired`, `NotAllowed`, `AccountError`; `normalize_username`, `TOKEN_SCOPES` |
| `src/steward/core/auth/passwords.py`, `common_passwords.txt` | argon2id `hash_password`/`verify_password`/`needs_rehash`/`dummy_verify` (NFKC, max 2 concurrent hashes), rules `problems`/`check` → `WeakPasswordError` |
| `src/steward/core/auth/tokens.py` | session secrets, `stw_` API tokens, `digest` (SHA-256), `hint`, `csrf_token`/`check_csrf` (HMAC of the session secret) |
| `src/steward/core/auth/limits.py` | login rate limiting over `login_attempts`: `check` → `Lockout(scope, until)`, `record`, `clear`, `failures_since`, `prune` |
| `src/steward/core/auth/clientip.py` | `client_ip(peer, x_forwarded_for, trusted=)`, `is_trusted_proxy(peer, trusted=)` |
| `src/steward/core/setup/` | `token.py` `SetupToken` (memory-only random token, `check`); `suggest.py` `suggest(path)` → `Suggestion(type_name, reason)` (name with number prefix stripped → marker files `.git`/`.obsidian`/… → file kinds of ≤2000 entries, depth 3, no symlinks), `suggest_by_name`; `wizard.py` steps `STEPS` (`owner`, `libraries`, `types`, `private`, `roles`, `done`), `progress(conn, has_owner=)` → `Progress(step, finished, private_folder, private_skipped)`, `advance`, `library_candidates(config, policy)`, `suggest_library_id`, `add_library` (absolute, existing dir, unique id, no overlapping roots; returns warnings), `import_config_files`, `top_level_folders(policy, lib)` → `TopFolder`s, `apply_types` (skips folders that already have a type), `choose_private(ref, create=, permanent=)`, `parse_new_folder`, `skip_private`, `apply_roles({role: ref or None})`, `SETUP_ROLES`, `SetupError` |
| `src/steward/core/executor/dirs.py` | `create_folder(root, relpath, mode=)`, `WRITE_LOCK` (process-wide single writer, used by `Executor`), `ExecutorError` |
| `src/steward/sdk/actions.py` | Action SDK: `LibraryPath`/`PathField`, `NodeKind`, `AiMode`, `ActionRefused`, `PathState` (`satisfied_by`, `as_dict`/`from_dict`), `Plan(action, params, summary, preconditions, reads, writes, data)`, `Applied(result, post, moves, changed)`, `Rendered`, `PathPolicy`, protocols `ReadOnlyFS` (`state`, `settings`, `file_id`, `trash_folder`, `backup_folder`, `free_name`, `now`), `GuardedFS` (`mkdir`, `rmdir`, `move`, `create`, `copy`, `replace`, `read`, `set_lock`, `set_folder_type`), `ActionType` (`name`, `params`, `ai_mode`, `sensitive`, `plan`, `render`, `apply`, `inverse`, `examples`), `EXAMPLE_TREE`. `Registry.action(t)` (core may use `file.*`/`folder.*`), `Registry.exporter(fn)`; `Registrations.actions()`, `.exporters()` |
| `src/steward/core/executor/guarded.py` | `ReadOnlyView` (policy + state_db + clock; `target(path, new=)`, `trash_folder` rules), `Guarded` (the `GuardedFS`; `allowed_writes` compared in NFC; DB ops deferred to the executor's transaction), `open_dir` (`O_NOFOLLOW` per component), `_rename_noreplace` (`renameat2`), `_link_new` (temp + fsync + link), `check_segments`, `content_hash`, `within`, `TEMP_PREFIX` (`.steward-tmp-`, also ignored by discovery), `LOCKED_TRASH` |
| `src/steward/core/executor/actions.py` | Core action types `file.mkdir` (parents, private → 0700), `file.mkdirs`, `file.rmdir`, `file.move` (parents, `remove_empty` for undo), `file.rename`, `file.delete` (sensitive; trash `<YYYY-MM-DD>/<relpath>`, locked sources → `trash/locked`), `file.create`, `file.edit` (expected hash, backup to `edit_backups/YYYY/MM/<file_id>/`), `folder.lock`, `folder.unlock` (sensitive); `BUILTIN_ACTIONS`, `register_actions`, `MAX_TEXT_BYTES` |
| `src/steward/core/executor/executor.py` | `Executor(dbs, actions, clock=, max_attempts=)`: `preview`, `run(name, params, actor=, confirmed=, proposal_id=)`, `undo(entry_id, actor=, confirmed=)`, `get`, `entries(limit=, before=)`; `JournalEntry` (`undoable`, `paths`), `Preview`, `ActionFailed`, `SensitiveActionError`. One transaction per action: DB ops, file/folder identities follow moves, journal row, `action.applied` event, `core.scan` jobs (priority 20) |
| `src/steward/core/privacy/` | `rules.py` `matches(relpath, pattern)`, `PrivacyRules` (`from_config`, `is_secret`, `level_for`); `classify.py` `Classifier` (`load(conn, rules)`, `for_path`, `for_files`) → `FileClass(sensitivity, locked, visible, shareable, known)`, `UNKNOWN`; `overrides.py` `set_override(conn, file_id, level|None, actor=)` (event `file.sensitivity_changed`) |
| `src/steward/core/llm/` | `transport.py` `ChatRequest`, `EmbedRequest`, `ModelResponse`, `ModelTransport`, `OpenAICompatibleTransport` (stdlib urllib), `ModelUnavailable`; `gateway.py` `Gateway(config, state_db, privacy, transport)` `.route(role)`, `.call(role, request, provenance=, purpose=, taint=, allow_local_fallback=)` → `GatewayResult`; `PrivacyViolation`, `RoleNotConfigured`, `check_llm_config` (refuses remote endpoints for now). Only this package may use `urllib`/`openai` |
| `src/steward/core/search.py` | `search_files(dbs, policy, query, filters=SearchFilters(...), scope="normal"|"locked", limit=, offset=)` → `SearchResults(hits, total, capped)`; raw read-only `sqlite3` with `index.db` attached as `idx`, SQL functions `steward_fold`/`steward_name` (NFC + casefold); `hidden_folders(policy)`, `Visibility(policy)` (`is_hidden`, `is_locked`, `path_hidden`, `path_locked`) used by listings, the journal and `/api/v1`; `SearchError` |
| `src/steward/core/backup.py` | `backup(runtime, reason=, keep=)` → `BackupInfo` (online-backup copies of both DBs, `state.json`, `manifest.json`, `.partial-` then rename, 0600, prune), `export_state(runtime, strict=)` (`CORE_EXPORT_TABLES` minus secret columns + module exporters), `list_backups`, `latest_backup`, `prune`, `before_migrate(runtime)` (pending revision on existing DBs and no backup within `max_age_before_migrate_hours` → back up or raise `BackupError`) |
| `src/steward/api/security.py` | The guard (app-wide dependency): `access(level)` decorator for core routes (`public`, `setup`, `session` default, `principal`), `level_of`, `ApiState(auth, api, trusted_proxies, setup_token)` (`has_owner` cached once true), `guard` (sets `request.state.principal/session_secret/csrf/presession/ip/https`), refusals `Refused(status, message)`, `NeedLogin`, `SetupRequired`, `SetupClosed`; `safe_next`, `is_https`; cookie names `steward_session`, `steward_setup`, `steward_presession` |
| `src/steward/api/app.py` | `create_app(runtime, setup_token=)`: core routers, module pages (`UiPageError` for reserved/duplicate paths or non-`APIRoute` routes), `/static`, `_check_routes` (fail closed), exception handlers (login redirect / 401 JSON, `/setup` redirect / 503, re-auth redirect, 429 + `Retry-After`, HTML or JSON errors), security headers middleware; `CORE_UI_PATHS` |
| `src/steward/api/templating.py` | `render(request, template, context, status=, headers=)` (adds `principal`, `csrf`, `form_token`, `nav`, `notice`; sets the pre-session cookie), `redirect` (303), `set_cookie`/`clear_cookie`, `NOTICES` (fixed `?notice=` messages), `NavEntry` |
| `src/steward/api/routes/` | `auth.py` (`/login`, `/logout`, `/reauth`), `setup.py` (`/setup`, `/setup/owner`, `/setup/libraries|types|private|roles|done`; `next_page`, `progress_of`), `settings.py` (`/ui/settings` + POST `/password`, `/sessions/revoke`, `/sessions/revoke-others`, `/login`, `/tokens`, `/tokens/revoke`), `home.py` (`/` → `/ui/`, `/ui/`, `/health`, `/api/v1/me`), `pages.py` (`/ui/search`, `/ui/journal` + POST `/{id}/undo`, `/ui/jobs` + POST `/{id}/retry`, `/ui/health`, `/ui/settings/folders` + POST `/map`, `/unmap`, `/lock`; `run_health_checks(runtime)`), `api_v1.py` (see "API v1" below) |
| `src/steward/api/server.py` | `serve(runtime)`: `backup.before_migrate`, `upgrade_databases`, setup token (logged as `setup token: …` while no owner), executor-lane `Worker` thread (`housekeeping=False`, id `api-…`), Uvicorn (`proxy_headers=False`, no access log, no server header) |
| `src/steward/ui/` | `TEMPLATES_DIR` (Jinja2: `base.html`, `error.html`, `login.html`, `reauth.html`, `home.html`, `settings.html`, `search.html`, `journal.html`, `jobs.html`, `health.html`, `folders.html`, `setup/*.html`), `STATIC_DIR` (`steward.css`, light/dark). No JavaScript; CSP forbids inline scripts and styles |
| `src/steward/core/auth/jobs.py` | `register_auth`: job + daily 04:30 schedule `core.auth.prune`, health check `core.auth` (`check_health`) |
| `src/steward/core/settings.py` | `settings_kv` store: `get_all(conn, prefix)`, `set_value` (emits `settings.changed`, no-op if unchanged), `clear` |
| `src/steward/core/logging.py` | JSON log formatter (`extra={"fields": {...}}`), logs exception type only |
| `src/steward/core/folders/paths.py` | `normalize_relpath` (NFC, `/`, no `.`/`..`/empty, ≤255-byte segments; root `""`), `ancestors`, `is_within`, `FolderRef(library_id, relpath)` (`parse("lib:path")`, `str()`), `PathError` |
| `src/steward/core/folders/types.py` | `TypeSettings` (all optional), `TypeDefinition` (+`extends`), `StageChanges`, `BUILTIN_TYPES` (13 types), `resolve_types(overrides, custom)` → `ResolvedType`s, `max_sensitivity`, `FolderTypeError` |
| `src/steward/core/folders/policy.py` | `Policy(libraries, types, mappings, roles)` snapshot: `settings_for(ref)` → `EffectiveSettings` (type, `type_from`, restrictions carried down, `locked_by`, `permanent`, `notes`), `role_at(ref)`; `Role`, `ROLES`, `COPY_HOLDING_ROLES` |
| `src/steward/core/folders/store.py` | DB ops (emit events): `load_policy`, `upsert_library`, `ensure_folder`, `folder_id`, `move_folder`, `define_type`/`rename_type`/`delete_type`, `set_type`, `lock`/`unlock`/`clear_lock_override`, `set_role`/`clear_role`, `ensure_default_roles`; `FolderError`, `PermanentLockError` |
| `src/steward/core/folders/seed.py` | `import_seed(conn, libraries=, folders=)` → `ImportReport`; additive, parents-first types |
| `src/steward/core/loader/loader.py` | `discover()` (entry points, no imports), `resolve_enabled(choices)` (defaults: built-in features on, all else off), `load_modules(enabled, entry_points=None, core=None)` → `LoadedModules(modules, registrations)`; error classes, all subclasses of `ModuleLoadError` |
| `src/steward/modules/` | Built-in modules go here (none yet) |
| `src/steward/cli.py` | Typer app, global `--config-dir`: `version`, `modules`, `api` (`steward.api.serve`), `config check`, `db upgrade`, `db status`, `jobs list`, `jobs retry <id>`, `worker`, `folders import|list|show|map|unmap|lock|unlock|role|move`, `types list|show|rename|delete`, `scan [lib[:path]…] [--allow-empty]`, `files list [lib:path] [--missing]`, `stages list`, `stages plan`, `user list|reset-password [NAME]|unlock [--username|--ip]|sessions [--all]|logout [ID]|settings [--reauth/--no-reauth] [--reauth-minutes N]`, `tokens list [--all]|create LABEL [--scope …] [--expires-days N]|revoke ID`, `files sensitivity LIB:PATH local-only|excluded|clear`, `search QUERY [--library --folder --kind --limit]`, `journal list [--limit]|undo ID` (host is trusted: `confirmed=True`), `backup [--keep N]`, `export [--output F]`; `db upgrade` backs up first via `before_migrate` (actor `cli`); `worker` also queues a startup `core.reconcile`, a full stage pass if the graph signature changed, and runs the `Watcher` thread |
| `config/*.example.yaml` | Annotated examples of `steward.yaml` (incl. `backup:`), `modules.yaml`, `pipeline.yaml`, `libraries.yaml`, `folders.yaml`, `privacy.yaml`, `llm.yaml` (tests validate and import them) |
| `examples/hello-module/` | Out-of-tree module `hello` 0.3.0 (dist `steward-hello`): config `greeting`; registers health check `hello`, stage `hello.lines` (text files ≤10 MiB → `hello_line_counts` in `index.db`, migration `migrations/index/0001`), subscriber `hello.greet` (`file.discovered`, logs), UI page `hello.page` at `/ui/hello`. Dev dependency only; not in the image |
| `tests/unit/test_auth_primitives.py`, `tests/integration/test_auth.py`, `test_auth_cli.py` | Password hashing/rules, secrets/CSRF, client IP, settings bounds; the `Auth` service end to end (sessions, expiry, lockouts, re-auth windows incl. 0 and off, tokens can't do session things, password change/reset, prune, health); CLI. Integration tests patch `passwords._hasher` with cheap parameters (fixture `fast_hashes`) |
| `tests/support.py` | Helpers (importable as `support`): `FakeEntryPoint`, `make_module(name, register=...)`, `eps`, `write_config`, `make_runtime(tmp_path, modules, upgrade=True, **files)`, `use_fast_hashes(monkeypatch)` |
| `tests/api_support.py` | Web test helpers (importable as `api_support`): `USERNAME`, `PASSWORD`, `SETUP_TOKEN`, `form_token(html)`, `make_owner(runtime, finished=True)`, `finish_setup`, `log_in(client)` → CSRF token, `csrf_of` |
| `tests/integration/test_api.py`, `test_api_security.py`, `test_api_server.py`, `test_setup_wizard.py`; `tests/unit/test_setup_suggest.py`, `test_executor_dirs.py` | HTTP flows with `TestClient(follow_redirects=False)` (setup mode, wizard end to end, login/logout, lockout 429, CSRF, cross-site, re-auth redirect, settings, bearer tokens, module pages); the security suite enumerates every route of the app (incl. module pages) and checks anonymous refusal (setup mode and after), CSRF on every unsafe route, tokens only on token routes, and that the public/setup/token route sets equal explicit lists; `serve` startup; wizard core functions; suggestions; folder creation safety |
| `tests/unit/` | Loader, registry, config, schedule parsing/timing, folder paths/types, folder policy (incl. the same expectations on two different layouts), hashing/MIME/ignore patterns, stage graph + `run_status` + stage registration checks (`test_stage_graph.py`) |
| `tests/integration/` | Migrations (incl. module prefix rule, disabled modules, schema drift), queue (leases, takeover, backoff, dedupe), events + scheduler, worker (end-to-end, crash takeover, threads), CLI, module discovery, folder store, discovery (`test_discovery.py`: identity rules, folder renames on two layouts, skips, safety, watcher debounce), stages (`test_stages.py`: order, dedupe by content, folder types add/remove/metadata, type changes, skip/fail/retry, files changing under a stage, version bumps, graph errors, the `hello` module enabled and disabled) |
| `scripts/check_headers.py` | Fails if a `.py` file doesn't start with the SPDX line |
| `deploy/Dockerfile` | Multi-stage, base images pinned by digest, `uv sync --locked --no-dev`, venv at `/opt/venv`, runs as 1000:1000, license texts in `/licenses/`, default `CMD steward --help` |
| `deploy/compose.example.yaml`, `deploy/example.env` | Generic two-service compose: `steward-api` (rw library mount, port 8420, health check on `/health` via python) and the read-only `steward-worker` (starts when the API is healthy, i.e. migrated) |
| `.github/workflows/ci.yml` | `make check` and an image build; actions pinned by commit SHA |
| `tests/executor_support.py`, `tests/executor/test_action_safety.py`, `tests/integration/test_executor.py` | `ExecEnv` (runtime + library + scan/map/lock helpers), `build_tree`, `snapshot`; the safety harness parametrized over every registered action's `examples` (`NO_EXAMPLES = {"folder.unlock"}`); executor flows (trash, locked trash, edits, journal, events, identities) |
| `tests/privacy/test_privacy_suite.py`, `tests/unit/test_privacy_rules.py` | Gateway with a fake transport: every plan §9.6 case, log scan for content, trust/remote config errors, only `core.llm` uses `urllib`/`openai`; rule matching |
| `tests/integration/test_search.py`, `test_backup.py`, `test_api_v1.py` | Search on two layouts (hidden/locked/trash excluded, filters, Unicode); backups, prune, export contents, pre-migrate backup; `/api/v1` and the new UI pages (tokens never see locked folders or change anything, session actions + undo, journal redaction, OpenAPI, folder unlock re-auth) |
| `docs/adr/` | ADRs 0001–0016, index, template |
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
- Registered names must be `<module>` or `<module>.<…>` (dotted snake_case); `core` and
  `steward` are reserved module names. The core registers its own items as owner `core`
  through the same `Registry`.
- Extension points so far: `health_check`, `job(name, handler, lane=)`,
  `subscribe(event_type, handler, name=)`, `schedule(name, when, job=, payload=)`,
  `stage(name, run, version=, produces=, requires=, lane=, applies_to=, default=,
  index=)`, `migrations(path, database=)`, `ui_page(name, router, path=, title=,
  nav=)`. Point keys in `Registrations`: `health_check`, `job`, `subscriber`,
  `schedule`, `stage`, `migrations:state`, `migrations:index`, `ui_page`.
- Default enablement: modules from the `steward-ai` distribution with kind `feature` are
  on unless `modules.yaml` says otherwise; everything else is off until enabled.
  Enabling a module that isn't installed fails; mentioning one only warns.
- Adding an extension point: add the interface to `steward.sdk`, a typed method on
  `Registry` that calls `self._add("<point>", name, item)`, a typed accessor on
  `Registrations` if the core needs one, re-export it in `steward/sdk/__init__.py`,
  tests, and a row in `docs/modules.md`.

### Folders (implemented; ADRs 0006, 0009)
- State tables (core revision `0002`): `libraries` (`library_id`, `name`, `root`,
  `position`), `folder_types` (all 13 built-ins inserted by the migration with
  `builtin=1`, `settings` = owner overrides; custom rows have `extends`, FK with
  `ON UPDATE CASCADE`), `folders` (identity; unique `(library_id, relpath)`; `present`,
  `first_seen`, `last_seen`, `missing_since` for discovery), `folder_mappings`
  (`type_name` FK RESTRICT, `locked` override, `permanent` ⇒ `locked=1`), `folder_roles`,
  `path_history` (see Discovery). All change rows carry `actor`.
- Effective settings: nearest mapped type; sensitivity max along the path; not
  writable/shareable/visible carry down; lock decided only at mapped folders (override,
  else own type), once locked always locked below; locked ⇒ not shareable, not a triage
  target; `trash`/`edit_backups` role folders ⇒ ≥ local-only, not shareable/triage.
- Built-in types add `visible` and `ignore` settings beyond the plan's list (`ignored`
  is hidden; `projects` ignores `.git`, `node_modules`, `target`, …).
- Default roles: `trash` → `<first library>:.steward/trash` (type `trash`),
  `edit_backups` → `.steward/backups` (type `archive`); only DB rows, the executor will
  create the directories.
- Events: `library.added|updated`, `folder_type.created|updated|renamed|deleted`,
  `folder.type_changed|locked|unlocked|moved|role_changed`. Re-applying an unchanged
  value emits nothing.
- Discovery sets `folders.present/first_seen/last_seen/missing_since/inode` and
  creates a folder row for every directory it finds.

### Discovery (implemented; ADR 0010)
- Tables: state revision `0003` adds `files` (`file_id` uuid hex, `library_id`,
  `relpath` exact bytes-as-UTF-8, `content_hash`, `hash_kind`, `size`, `mtime_ns`,
  `inode`, `present`, `first_seen`, `last_seen`, `missing_since`, `error` = exception
  type if unreadable) and `folders.inode`; index revision `0002` adds `blobs`
  (`content_hash` PK, `hash_kind`, `size`, `mime`, `kind`) and `stage_runs` (PK
  `content_hash`+`stage`). `path_history` rows have `folder_id` xor `file_id` and
  old/new library + relpath.
- Identity order: same path → inode+size+mtime of a vanished row (same library) →
  content hash of a vanished row (any library; sampled also needs size+mtime) → new.
  Folders: inode of a vanished folder + at least one of its files inside (if it had any).
- A file is re-hashed only if size/mtime/inode changed or its hash is weaker than the
  folder's policy wants (`sampled` → `full`). A file that changes during hashing is
  skipped (`unstable`) until the next scan.
- Write pattern: hashing outside transactions; one short write transaction per changed
  file (index.db blob first, then state.db row + event); unchanged files are touched in
  batches of 500; long scans renew the job lease every 60 s.
- Effective settings come from `Policy.settings_for` (cached per folder for one scan):
  `index: none` folders aren't walked; type `ignore` + `discovery.ignore_patterns` skip names.
- Events: `file.discovered|changed|moved|reappeared|missing`, `folder.moved` (via store).

### Stages (implemented; ADR 0011)
- Stage names are `<module>.<name>`; folder-type `stages.add/remove` match full or short
  names (`Graph.matching`). Root stage `core.hash` (produces `hash`, `mime`; lane `io`;
  `index="metadata"`) is computed by discovery (`scanner._record_blob` writes its
  `stage_runs` row) and never queued; `HASH_STAGE` lives in `discovery/hashing.py`.
- Graph built in `build_runtime` after module loading; errors are `StageGraphError`.
- `stage_runs` (index): one row per (content_hash, stage); `error` holds the failure or
  the skip reason. Index revision `0003` adds `pipeline_meta(key, value, updated_at)`
  (key `stage_graph_signature`).
- current/usable rules: `run_status` (root: usable if `done`; others: same version, all
  upstream usable, upstream `updated_at` ≤ own). Wanted: `Eligibility.enabled_stages`
  (index none → nothing; `default or added`, minus removed; `index="full"` stages need
  folder `index: full`) and `applies_to` (exceptions → not applicable, logged).
- Jobs: `core.stage.<name>` (stage lane, payload `content_hash`, dedupe
  `stage:<name>:<hash>`); `core.stages.plan` (cpu, dedupe `core.stages.plan`; reads
  events after cursor `core.stages.planner` in `event_cursors`, batches of 1000, loops
  until caught up; `file.discovered|changed|moved|reappeared` → plan those files;
  `folder.type_changed|moved`, `folder_type.*` → full pass); `core.stages.full` (cpu,
  dedupe `schedule:core.stages.full`, also a daily 04:20 schedule; writes the signature
  when done). The planner cursor starts at 0.
- Stage handler: blob gone / inputs not usable / no candidate file → return without a
  row; already current (and not failed) → re-plan and return; `FileChanged` (from
  `open()` or the post-run `unchanged()` check) → no row, queue `core.scan` of that path;
  exception → `failed` row, re-raise (queue retries; `PermanentJobError` → dead); else
  `done`/`skipped` row, then re-plan the hash if the stage has dependents.
- The first housekeeping on a DB with old events plans every retained file event once
  (cheap; dedupe stops duplicates).

### Auth (implemented as a service; ADR 0012)
- State revision `0004`: `users` (normalized unique `username`, `role` = `owner` only,
  argon2id `password_hash`, `password_changed_at`, `disabled_at`), `sessions`
  (`token_hash` = SHA-256 of the cookie secret, `reauth_at`, `ip`, `user_agent`,
  `revoked_at`/`revoked_reason`), `api_tokens` (`secret_hash`, `hint`, `scopes` JSON,
  `expires_at`, `last_used`, `last_ip`, `revoked_at`), `login_attempts` (`username`,
  `ip`, `kind` login/reauth, `success`, `at`), `settings_kv` (`key`, JSON `value`).
- Session expiry is computed on each request from `created_at`/`last_seen` and the
  current `steward.yaml` values; `last_seen`/`last_used` written at most once a minute.
- Re-auth: `reauth_at` is set by login, `reauth`, `change_password`. `require_recent_auth`
  (session only; tokens get `NotAllowed`) passes if re-auth is off, or `reauth_at` is
  within `auth.reauth_minutes`; with 0 it passes within 2 min (`ONE_SHOT_REAUTH`) and
  clears `reauth_at`. Invalid stored settings raise `AuthError` (fail closed).
- Rate limiting: failures counted per `ip` and per `username`, within the window and
  with `attempt_id` greater than the key's last success; locked → `LockedOut` without
  verifying or recording. Unknown usernames are recorded too (dummy verify for timing).
- Password hashing always happens outside DB transactions.
- Events: `auth.user_created`, `auth.login`, `auth.login_failed`, `auth.locked_out`,
  `auth.session_revoked`, `auth.password_changed`, `auth.token_created`,
  `auth.token_revoked`, `settings.changed`. Payloads hold ids, usernames, addresses,
  never passwords or secrets.
- HTTP side: see "Web API and setup" below.

### Web API and setup (implemented; ADR 0013)
- `steward.api` may import `steward.core`; core, modules and out-of-tree modules may not
  import `steward.api` (import contracts).
- Every route goes through `security.guard` (app dependency). Levels: `public`
  (`GET /health`, `GET|POST /login`, `GET|POST /setup`), `setup` (`/setup/owner`: no
  owner yet + `steward_setup` cookie = token), `principal` (`/api/v1/me`), and
  `session` (default, everything else, all module pages). New core routes need
  `@access(...)` *below* the router decorator only if they aren't `session`, and then
  `PUBLIC`/`SETUP_ONLY`/`TOKEN_OK` in `tests/integration/test_api_security.py` must be
  updated (the test compares them).
- Order in the guard: refuse unsafe `Sec-Fetch-Site: cross-site` → authenticate (bearer
  header wins; an invalid one is refused, never falls back to the cookie) → read the
  form (unsafe methods only; cached for the endpoint) → level checks → CSRF (session:
  `check_csrf(secret, form csrf_token | X-CSRF-Token)`; no session on public/setup forms:
  double-submit `steward_presession`).
- Without an owner every non-public route → `/setup` (UI) or 503 (API). After the owner
  exists, `/setup/owner` → `/login`. Home and login send the owner to the next wizard
  step until `setup.finished`. Wizard steps redirect to `/ui/` once finished.
- Sync endpoints (`def`) run in the threadpool (auth calls block on argon2/SQLite); the
  guard is async and uses `run_in_threadpool`. `ReauthRequired` → 303
  `/reauth?next=<GET path or same-site Referer>`.
- Templates get `form_token` (`csrf` if logged in, else the pre-session token) for the
  hidden `csrf_token` field; messages after redirects use fixed `?notice=` keys.
- Settings in `settings_kv`: `setup.step`, `setup.finished`, `setup.private_folder`,
  `setup.private_skipped` (emit `settings.changed`).
- FastAPI docs/OpenAPI routes are disabled (they'd be outside the guard).

### Privacy, executor, search, API v1, backups (implemented; ADRs 0014–0016)
- State revision `0005`: `file_sensitivity` (per-file override, `local-only`/`excluded`)
  and `journal` (`entry_id`, `action`, `params`, `plan`, `result`, `inverse` JSON,
  `summary`, `sensitive`, `actor`, `proposal_id`, `undo_of`/`undone_by` FKs,
  `applied_at`).
- Config: `privacy.yaml` (`PrivacyConfig`), `llm.yaml` (`LlmConfig`), `backup:` in
  `steward.yaml` (`BackupConfig`: `keep` 7, `dir` default `<data_dir>/backups`,
  `max_age_before_migrate_hours` 24).
- Excluded files: scanner records them without hashing (`_excluded_file`, hash cleared);
  `Eligibility` gives excluded folders/files no stages.
- Executor decisions: sensitive = type flag or any path in a locked folder; sensitive
  without `confirmed` → `SensitiveActionError` (UI/API then call
  `require_recent_auth` and retry with `confirmed=True`; CLI is trusted). Actions run
  synchronously under `WRITE_LOCK` in the caller's thread.
- Visibility: normal views exclude locked, `visible: false`, `trash`-type and
  copy-holding role folders (outermost refs only), in SQL. Journal entries touching a
  locked folder show `LOCKED_SUMMARY` unless the session `is_recent`.
- API v1 (`api/routes/api_v1.py`, prefix `/api/v1`): `@access("principal")` + `read`
  scope: `GET libraries`, `folders?path=`, `files?path=`, `files/{id}`, `search`
  (`locked=true` needs session + recent auth), `actions`, `journal`, `jobs` (no
  payloads; `last_error` only for sessions), `health`, `openapi.json` (built from
  `/api/` routes, cached on `app.state`). Session-only (default level):
  `POST actions/{name}/preview`, `POST actions/{name}` (201), `POST journal/{id}/undo`,
  `POST jobs/{id}/retry`. Hidden paths answer 404. `TOKEN_OK` in
  `test_api_security.py` lists the token routes.
- Backups: `serve()` and `steward db upgrade` call `backup.before_migrate` before
  `upgrade_databases`; `core.backup` is registered by `register_backup` after modules
  so its job sees every exporter (it uses the built runtime via a closure).

### Jobs, events, schedules, migrations (implemented; ADRs 0007, 0008)
- State tables (core revision `0001`): `jobs`, `events`, `event_cursors`, `schedules`;
  version tables `alembic_version_<owner>`.
- Every DB function takes a `Connection`; writes go in `db.write()` (`BEGIN IMMEDIATE`).
  Timestamps are aware UTC (`UTCDateTime`); naive datetimes raise.
- Job statuses `queued` → `running` → `done` | back to `queued` (backoff) | `dead`.
  Completion/failure/renew check `status='running' AND worker_id=<holder>`. Expired
  leases are reclaimed with `last_error = "lease expired…"`; on the last attempt they go
  `dead`. Unique `dedupe_key` among queued/running jobs.
- Events: `emit(conn, type, payload, actor=)`; dispatcher keeps `event_cursors`; new
  subscribers start at the newest event; deliveries are jobs `core.deliver.<subscriber>`
  on lane `events`, dedupe `event:<id>:<subscriber>`.
- Schedules: first sight of an `Every` runs immediately, `Daily` waits for its time
  (in `steward.yaml` timezone); spec changes recompute from the last run; dedupe
  `schedule:<name>`.
- Worker IDs are `worker-<hostname>-<pid>-<random>`. Logs are JSON with `fields`.

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
- Alembic revision directories (`core/db/versions/`, any module's `migrations/`) are
  excluded from mypy (their file names start with digits).
- Tests import helpers from `tests/support.py` as `support`. Manual runs, generated
  config and data live outside the repo, never inside it.
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
1. ~~Core~~ *(done)*: config, DBs, queue, events, libraries/folder types, discovery, auth, setup
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
uv run steward --config-dir DIR config check   # or set STEWARD_CONFIG_DIR
uv run steward db upgrade | db status | jobs list | jobs retry ID | worker
uv run steward api                                  # web UI + setup wizard (port from steward.yaml)
uv run steward folders import | scan | files list   # libraries/folders, discovery
uv run steward stages list | stages plan            # pipeline stages
uv run steward user list | reset-password | unlock | sessions | settings
uv run steward tokens list | create LABEL --scope read | revoke ID
uv run steward search WORDS | journal list | journal undo ID | files sensitivity LIB:PATH LEVEL
uv run steward backup | export --output F            # backups (also nightly + before migrations)
make check                      # lint typecheck imports test headers licenses
make format                     # ruff format + ruff check --fix
uv run pytest tests/unit -q     # a subset of tests
uv run steward modules          # list installed modules and registrations
make notices                    # regenerate THIRD_PARTY_NOTICES.md
make image && docker run --rm steward:dev steward version
```
uv may be installed at `~/.local/bin/uv`; make sure it's on `PATH`.
