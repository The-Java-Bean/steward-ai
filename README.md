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
- **File manager in the browser** *(implemented)*: browse, preview, upload (large files,
  resumable), download, move, rename and delete (to a trash folder, with undo).
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

**Search** finds files whose path contains every word (case is ignored; accents
must match), names first, newest first, with filters for kind (document, image, ...),
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

### File manager *(implemented)*
**Files** (`/ui/files`) is your drive in the browser. Browse by folder, or by kind
(images, videos, documents, …) across every library. Click a file to preview it
(images, PDFs, text, audio and video play in the page) or download it; download a whole
folder, or the files you tick, as one zip that starts right away however big it is.
Make folders, rename, move (pick the destination folder) and delete; each change shows
an **Undo** button, and everything is in the Journal. Deleting moves things to the
trash and asks for your password (subject to your re-auth setting). Locked and hidden
folders and the trash don't appear here (locked folders are in the Locked Folder,
below), symlinks are never shown or followed, and search results link straight to
their files. The
file manager is a built-in module, `files_ui`; turn it off in `modules.yaml` with
`files_ui: {enabled: false}`.

**Uploading:** drop files on a folder's page, or click *Choose files*. Files of any
size go up in pieces (8 MiB by default), so a dropped connection, a laptop going to
sleep or a restart of steward-ai only pauses them: they carry on by themselves, and if
you close the browser, choosing the same file again in the same folder picks up where
it stopped (the folder page lists unfinished uploads, which you can also cancel). An
upload never replaces an existing file: if the name is taken, you can keep both (the
new one gets a name like `report (2).pdf`) or skip it. Each upload is in the Journal,
and undoing it moves the file to the trash. Uploading needs JavaScript. Unfinished
uploads are removed after 24 hours (`uploads.stale_hours`). Behind a reverse proxy,
allow request bodies of at least the piece size (nginx: `client_max_body_size 16m;`).
Scripts can use the same protocol at `/api/v1/uploads` (see the API section).

**Locked Folder** (`/ui/locked`, in the menu) works like a phone's locked photo album.
Any folder can be locked: open it in Files and choose *Lock folder…*, or give it a
locked folder type such as `private` (the private folder you chose at setup is
locked this way). A locked folder and everything inside it disappears from Files,
search, the journal's details, share links and (later) chat. Opening the Locked
Folder asks for your password (subject to your re-auth setting); inside it is the
normal file manager: upload, new folder, rename, move, delete (to a locked part of
the trash), preview, download and zip, files by kind, and a search of the locked
folders only. Nothing moves on disk and nothing is encrypted, so programs that read
the disk directly can still see locked folders; keep them out of those programs'
mounts.

- **Permanent locks:** tick *Make the lock permanent* when locking (or choose *Make
  the lock permanent…* inside the Locked Folder). A permanent lock can't be removed
  from the web pages, the API or by the AI, only with
  `steward folders unlock --permanent library:path` on the server. It asks for your
  password and can't be undone.
- **Unlocking** (inside the Locked Folder, *Unlock…*) first shows what would become
  visible: how many files and folders, a sample, and folders inside that keep a lock
  of their own. It asks for your password again. A folder locked because a parent is
  locked can only be unlocked with that parent.
- **Moving things out** of the Locked Folder: *Move…*, then *Out of the Locked
  Folder…*, pick the folder, and tick the confirmation. It asks for your password.

### Share links *(implemented)*
Share a file or a folder from Files: *Share* on a file's row or page, *Share folder…*
on a folder. Choose how long the link works (1, 7 or 30 days, any number of days, or
no expiry) and whether it needs a password (on by default, with a suggested one you
can change). After creating, the page shows the link (with a *Copy* button) and the
password once; send them separately. Anyone with the link sees a simple page without
your menus: the file with a preview and a download button, or the folder with its
subfolders, single-file downloads and a zip of everything.

- **What can't be shared:** whole libraries, anything in a locked or hidden folder,
  files or folders marked local-only or excluded (including secrets such as `*.key`),
  and folders whose type doesn't allow sharing. These rules are checked again on every
  visit: lock a shared folder or mark a file local-only and it stops being served at
  once. Inside a shared folder, such files and subfolders are simply left out.
- **Folders:** files added later are shared too, unless you tick *Snapshot* (only the
  files steward-ai has scanned there now).
- **No expiry** is allowed, but asks for your password, shows a warning, and the menu
  shows how many such links exist next to *Shares*.
- **Shares** (`/ui/shares`) lists every link: *Active* (link, expiry, password on/off,
  how often it was opened and when last, who made it) and *Past* (expired, revoked or
  undone, with when). Filter by path and age. Each share has a page with its access log
  (time, address, browser, wrong passwords) where you can change the expiry or password
  (a new password logs out everyone who entered the old one) or revoke it. Revoking is
  final; undoing a new share (the *Undo* button after creating it, or the Journal)
  turns its link off. Items that are now in a locked folder are listed without their
  name.
- **Passwords:** a correct password is remembered in that browser for 12 hours. After
  10 wrong passwords from one address (or 100 in all) in 15 minutes, the link waits a
  while before trying again.
- Expired, revoked and unknown links all show the same "isn't available" page.
- Behind a reverse proxy, set `api.public_url` in `steward.yaml` (for example
  `https://files.example.com`) so the links use that address. Share links are the only
  pages that work without logging in; if you publish steward-ai through an SSO proxy,
  let `/s/` through without it (and `/static/`, which only holds styles and scripts).
- Shares are a built-in module, `shares`; with `shares: {enabled: false}` in
  `modules.yaml` every link stops working.

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
can read; running actions, undo, retrying jobs and uploads (`/api/v1/uploads`: `POST`
to start, `PUT` pieces with an `Upload-Offset` header, `POST …/finish`) need a
logged-in session with its CSRF token in `X-CSRF-Token`. Locked folders never appear in API answers to tokens.

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
- **Phase 2 (files, Locked Folder, shares) in progress: chunks 1–4 of 5 done** (1:
  module web SDK + `files_ui` file manager, ADR 0017; 2: resumable uploads, ADR 0018;
  3: the Locked Folder `/ui/locked`, ADR 0019; 4: share links, the `shares` module and
  the core `/s/` route, ADR 0020). Next: chunk 5 acceptance + test environment.
- **Phase 0 done. Phase 1 (core): all 6 chunks done** (1: config, databases and migrations, job queue, events, scheduler,
  worker; 2: libraries, folder types, mappings, locks, roles, folder identity; 3:
  discovery: scanner, watcher, hashing, file identity; 3b: stage SDK, graph, planner,
  stage jobs, ADR 0011; 4: auth service, ADR 0012; 5: `steward api`, HTTP auth layer,
  setup wizard, settings page, `ui_page` extension point, ADR 0013; 6: privacy
  classification + model gateway (ADR 0015), executor/journal/undo + safety harness
  (ADR 0014), keyword search, `/api/v1`, `/ui/search|journal|jobs|health`,
  `/ui/settings/folders`, `steward backup|export` + pre-migration backups (ADR 0016)).
- Not yet: `/api/v1/shares`, other share providers, moving files into the Locked Folder from Files, a trash view in the file manager, `steward setup --from`, `SetupStep` for modules, the model-endpoint setup
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
| `src/steward/sdk/jobs.py` | `Lane` (`io`, `cpu`, `llm-local`, `llm-remote`, `events`, `executor`), `LANES`, `WORKER_LANES`, `JobContext(job_id, type, payload, attempt, renew_lease, state)`, `JobHandler`, `PermanentJobError` |
| `src/steward/sdk/events.py` | `Event(event_id, type, version, payload, actor, occurred_at)`, `EventHandler` |
| `src/steward/sdk/stages.py` | `Stage` (name, version, produces, run, requires, lane, applies_to, default, index; `short_name`), `StageContext` protocol (`stage`, `blob`, `file`, `folder`, `attempt`, `open()`, `index_read()`, `index_write()`, `renew_lease()`), `StageResult(status, reason, model, prompt_version)`, `BlobInfo`, `FileInfo`, `FolderInfo`, `FileChanged`, `StageRunner`, `AppliesTo`, literals `IndexPolicy`, `Sensitivity` (also used by `core.folders.types`), `StageStatus`, `ARTIFACT_PATTERN` |
| `src/steward/sdk/ui.py` | `UiPage(path, router, title, nav, templates, static, badge)`, `Badge` (`(StateAccess) -> int`, shown next to the nav entry when not 0), `UI_PATH_PATTERN` (`/ui/<slug>`); `Registry.ui_page(name, router, *, path, title, nav=True, templates=, static=, badge=)`, `Registrations.ui_pages()` (point `ui_page`) |
| `src/steward/sdk/state.py` | Module state access: `StateAccess` protocol (`read()`/`write()` → SQLAlchemy `Connection`; the core's `Database` fits), `UTCDateTime` (moved here from the core; `core.db.types` re-exports it), `utcnow()` |
| `src/steward/sdk/shares.py` | Share links (ADR 0020): `TOKEN_PATTERN` (22 URL-safe chars), `ShareRequest` protocol (`token`, `subpath`, `method`, `query`, `form`, `client_ip`, `user_agent`, `state`, `form_token`, `base_url`, `now`, `cookie`, `open(path, kind, only_files=)` → `SharedItem`, `render` (module template, `public.html` layout), `redirect` (under `base_url`), `set_cookie` (path-scoped, Lax), `not_found`), `SharedItem` (`name`, `kind`, `entry`, `listing` → `SharedListing`, `read_text`, `file_response`, `zip_response`; paths relative to the item), `SharedListing`, `ShareLinkHandler`; `Registry.share_links(handler)` (point `share_links`, name = module), `Registrations.share_links()` |
| `src/steward/sdk/schedules.py` | `Every(timedelta)` (≥1 min), `Daily(time)`, `When`; `str()` gives `every 6h` / `daily 03:00` |
| `src/steward/core/config/config.py` | `load_config(dir)` → `Config(steward, modules, pipeline)`; models `StewardConfig` (`data_dir`, `timezone`, `log_level`, `trusted_proxies`, `auth: AuthConfig` = `session_idle_hours`, `session_max_days`, `login: LoginLimits`; `api: ApiConfig` = `host` 0.0.0.0, `port` 8420, `secure_cookies` auto/always/never, `public_url` (share links); `setup: SetupConfig` = `library_dirs` (`/libraries`)), `ModulesConfig` (`modules: {name: {enabled, settings}}`), `PipelineConfig` (`lanes`, `jobs`, `events`, `discovery: DiscoverySettings`, `schedules`); `ConfigError`; env `STEWARD_CONFIG_DIR` (default `/config`) |
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
| `src/steward/sdk/actions.py` | Action SDK: `LibraryPath`/`PathField`, `NodeKind`, `AiMode`, `ActionRefused`, `PathState` (`satisfied_by`, `as_dict`/`from_dict`), `Plan(action, params, summary, preconditions, reads, writes, data, sensitive, transient)` (`transient`: never serialized/journaled; undo plans lack it), `Applied(result, post, moves, changed)`, `Rendered`, `PathPolicy` (`type`, `writable`, `locked`, `sensitivity`, `lock_from`, `permanent`), `PathPrivacy(sensitivity, locked, hidden, shareable, reason)`, protocols `ReadOnlyFS` (`state`, `settings`, `file_id`, `trash_folder`, `backup_folder`, `free_name`, `privacy`, `read_state`, `file_ids_in`, `now`), `GuardedFS` (`actor`, `mkdir`, `rmdir`, `move`, `create`, `copy`, `replace`, `read`, `set_lock(path, locked, permanent=)`, `set_folder_type`, `write_state(op)`), `ActionType` (`name`, `params`, `ai_mode`, `sensitive`, `plan`, `render`, `apply`, `inverse`, `examples`), `EXAMPLE_TREE`. `Registry.action(t)` (core may use `file.*`/`folder.*`), `Registry.exporter(fn)`; `Registrations.actions()`, `.exporters()` |
| `src/steward/core/executor/guarded.py` | `ReadOnlyView` (policy + state_db + clock; `target(path, new=)`, `trash_folder` rules), `Guarded` (the `GuardedFS`; `allowed_writes` compared in NFC; `actor` set by the executor; DB ops incl. `write_state` deferred to the executor's transaction); `ReadOnlyView` takes `rules` (`PrivacyRules`) for `privacy()`, `open_dir` (`O_NOFOLLOW` per component), `_rename_noreplace` (`renameat2`), `_link_new` (temp + fsync + link), `check_segments`, `content_hash`, `within`, `TEMP_PREFIX` (`.steward-tmp-`, also ignored by discovery), `LOCKED_TRASH` |
| `src/steward/core/executor/actions.py` | Core action types `file.mkdir` (parents, private → 0700), `file.mkdirs`, `file.rmdir`, `file.move` (parents, `remove_empty` for undo), `file.rename`, `file.delete` (sensitive; trash `<YYYY-MM-DD>/<relpath>`, locked sources → `trash/locked`), `file.create`, `file.edit` (expected hash, backup to `edit_backups/YYYY/MM/<file_id>/`), `folder.lock` (`LockParams` path + `permanent`; permanent → `Plan.sensitive`, no inverse; can make an own lock permanent), `folder.unlock` (`UnlockParams`; sensitive; refuses permanent and inherited locks at plan time); `BUILTIN_ACTIONS`, `register_actions`, `MAX_TEXT_BYTES` |
| `src/steward/core/executor/executor.py` | `Executor(dbs, actions, clock=, max_attempts=, rules=)` (`Runtime.executor` passes `runtime.privacy`; `_view(policy)`) (sensitive = type flag, `plan.sensitive` or a locked path): `preview`, `run(name, params, actor=, confirmed=, proposal_id=)`, `undo(entry_id, actor=, confirmed=)`, `get`, `entries(limit=, before=)`; `JournalEntry` (`undoable`, `paths`), `Preview`, `ActionFailed`, `SensitiveActionError`. One transaction per action: DB ops, file/folder identities follow moves, journal row, `action.applied` event, `core.scan` jobs (priority 20) |
| `src/steward/core/privacy/` | `rules.py` `matches(relpath, pattern)`, `PrivacyRules` (`from_config`, `is_secret`, `level_for`); `classify.py` `Classifier` (`load(conn, rules)`, `for_path`, `for_files`) → `FileClass(sensitivity, locked, visible, shareable, known)`, `UNKNOWN`; `overrides.py` `set_override(conn, file_id, level|None, actor=)` (event `file.sensitivity_changed`); `sharing.py` `SharingRules(policy, rules, overrides by (lib, relpath))` (`load(conn, policy, rules)`, cached `check(lib, relpath)` → `PathPrivacy`: path judged as file (parent settings) and folder (own), `Visibility.is_hidden`, rules, overrides; reasons), `HIDDEN` |
| `src/steward/core/llm/` | `transport.py` `ChatRequest`, `EmbedRequest`, `ModelResponse`, `ModelTransport`, `OpenAICompatibleTransport` (stdlib urllib), `ModelUnavailable`; `gateway.py` `Gateway(config, state_db, privacy, transport)` `.route(role)`, `.call(role, request, provenance=, purpose=, taint=, allow_local_fallback=)` → `GatewayResult`; `PrivacyViolation`, `RoleNotConfigured`, `check_llm_config` (refuses remote endpoints for now). Only this package may use `urllib`/`openai` |
| `src/steward/core/search.py` | `search_files(dbs, policy, query, filters=SearchFilters(...), scope="normal"|"locked", limit=, offset=)` → `SearchResults(hits, total, capped)` (locked scope = locked folders minus `concealed`); raw read-only `sqlite3` with `index.db` attached as `idx`, SQL functions `steward_fold`/`steward_name` (NFC + casefold); `hidden_folders(policy)`, `_folder_sets`, `Visibility(policy)` (`hidden`, `locked`, `concealed` = hidden for non-lock reasons; `is_hidden`, `is_locked`, `in_locked_view`, `locked_roots()`, `path_hidden`, `path_locked`) used by listings, the journal and `/api/v1`; `SearchError` |
| `src/steward/core/backup.py` | `backup(runtime, reason=, keep=)` → `BackupInfo` (online-backup copies of both DBs, `state.json`, `manifest.json`, `.partial-` then rename, 0600, prune), `export_state(runtime, strict=)` (`CORE_EXPORT_TABLES` minus secret columns + module exporters), `list_backups`, `latest_backup`, `prune`, `before_migrate(runtime)` (pending revision on existing DBs and no backup within `max_age_before_migrate_hours` → back up or raise `BackupError`) |
| `src/steward/api/security.py` | The guard (app-wide dependency): `access(level)` decorator for core routes (`public`, `setup`, `session` default, `principal`), `level_of`, `ApiState(auth, api, trusted_proxies, setup_token)` (`has_owner` cached once true), `guard` (sets `request.state.principal/session_secret/csrf/presession/ip/https`), refusals `Refused(status, message)`, `NeedLogin`, `SetupRequired`, `SetupClosed`; `safe_next`, `is_https`; cookie names `steward_session`, `steward_setup`, `steward_presession` |
| `src/steward/api/app.py` | `create_app(runtime, setup_token=)`: core routers, module pages (`UiPageError` for reserved/duplicate paths, non-`APIRoute` routes or two templates/static dirs per module; module static at `/static/m/<module>` mounted before `/static`; sets `app.state.module_pages`, `templates`, `steward_web`, `link_builders`), `/static`, `NotFound` → 404, `_check_routes` (fail closed), exception handlers (login redirect / 401 JSON, `/setup` redirect / 503, re-auth redirect, 429 + `Retry-After`, HTML or JSON errors), security headers middleware; `CORE_UI_PATHS`; also `app.state.share_links` = `(module, handler)` or None (`_share_links`: more than one → `UiPageError`), `app.state.page_paths`, nav entries carry badges |
| `src/steward/api/templating.py` | `make_templates(module_dirs)` (per-app env: core templates + `PrefixLoader` `<module>/…`, stored as `app.state.templates`), `render(request, template, context, status=, headers=)` (adds `principal`, `csrf`, `form_token`, `nav`, `notice`; sets the pre-session cookie), `redirect` (303), `set_cookie`/`clear_cookie`, `NOTICES` (fixed `?notice=` messages), `NavEntry(path, title, badge)`, `nav(request)` → `NavItem(path, title, count)` (badge errors logged, count 0) |
| `src/steward/api/routes/` | `auth.py` (`/login`, `/logout`, `/reauth`), `setup.py` (`/setup`, `/setup/owner`, `/setup/libraries|types|private|roles|done`; `next_page`, `progress_of`), `settings.py` (`/ui/settings` + POST `/password`, `/sessions/revoke`, `/sessions/revoke-others`, `/login`, `/tokens`, `/tokens/revoke`), `home.py` (`/` → `/ui/`, `/ui/`, `/health`, `/api/v1/me`), `pages.py` (`/ui/search`, `/ui/journal` + POST `/{id}/undo`, `/ui/jobs` + POST `/{id}/retry`, `/ui/health`, `/ui/settings/folders` + POST `/map`, `/unmap`, `/lock`; `run_health_checks(runtime)`), `api_v1.py` (see "API v1" below) |
| `src/steward/api/server.py` | `serve(runtime)`: `backup.before_migrate`, `upgrade_databases`, setup token (logged as `setup token: …` while no owner), executor-lane `Worker` thread (`housekeeping=False`, id `api-…`), Uvicorn (`proxy_headers=False`, no access log, no server header) |
| `src/steward/ui/` | `TEMPLATES_DIR` (Jinja2: `base.html` (blocks `title`, `head`, `content`; nav badges), `public.html` (share pages: no nav, blocks `title`, `head`, `heading`, `content`), `share_missing.html`, `error.html`, `login.html`, `reauth.html`, `home.html`, `settings.html`, `search.html`, `journal.html`, `jobs.html`, `health.html`, `folders.html`, `setup/*.html`), `STATIC_DIR` (`steward.css`, light/dark). No JavaScript; CSP forbids inline scripts and styles |
| `src/steward/core/auth/jobs.py` | `register_auth`: job + daily 04:30 schedule `core.auth.prune`, health check `core.auth` (`check_health`) |
| `src/steward/core/settings.py` | `settings_kv` store: `get_all(conn, prefix)`, `set_value` (emits `settings.changed`, no-op if unchanged), `clear` |
| `src/steward/core/logging.py` | JSON log formatter (`extra={"fields": {...}}`), logs exception type only |
| `src/steward/core/folders/paths.py` | `normalize_relpath` (NFC, `/`, no `.`/`..`/empty, ≤255-byte segments; root `""`), `ancestors`, `is_within`, `FolderRef(library_id, relpath)` (`parse("lib:path")`, `str()`), `PathError` |
| `src/steward/core/folders/types.py` | `TypeSettings` (all optional), `TypeDefinition` (+`extends`), `StageChanges`, `BUILTIN_TYPES` (13 types), `resolve_types(overrides, custom)` → `ResolvedType`s, `max_sensitivity`, `FolderTypeError` |
| `src/steward/core/folders/policy.py` | `Policy(libraries, types, mappings, roles)` snapshot: `settings_for(ref)` → `EffectiveSettings` (type, `type_from`, restrictions carried down, `locked_by`, `permanent`, `notes`), `role_at(ref)`; `Role`, `ROLES`, `COPY_HOLDING_ROLES` |
| `src/steward/core/folders/store.py` | DB ops (emit events): `load_policy`, `upsert_library`, `ensure_folder`, `folder_id`, `move_folder`, `define_type`/`rename_type`/`delete_type`, `set_type`, `lock`/`unlock`/`clear_lock_override`, `set_role`/`clear_role`, `ensure_default_roles`; `FolderError`, `PermanentLockError` |
| `src/steward/core/folders/seed.py` | `import_seed(conn, libraries=, folders=)` → `ImportReport`; additive, parents-first types |
| `src/steward/core/loader/loader.py` | `discover()` (entry points, no imports), `resolve_enabled(choices)` (defaults: built-in features on, all else off), `load_modules(enabled, entry_points=None, core=None)` → `LoadedModules(modules, registrations)`; error classes, all subclasses of `ModuleLoadError` |
| `src/steward/sdk/web.py` | Web services for module pages (ADR 0017): `WebDep` (FastAPI dependency → `get_web` → `app.state.steward_web`), protocols `Web` (`viewer`, `module`, `files`, `actions`, `csrf_token`, `static_url`, `render`, `redirect`, `recently_authenticated`, `require_reauth`), `Files` (`libraries`, `listing`, `entry`, `by_kind(scope=)`, `search`, `locked_folders`, `unlock_preview`, `read_text`, `file_response`, `zip_response`; `scope` `normal`/`locked`), `Actions` (`preview`, `run`, `run_all`, `undo`); dataclasses `Viewer`, `LibraryInfo`, `Crumb`, `Entry`, `FolderListing` (+`lock_from`), `FilePage` (alias `KindPage`), `LockedFolder`, `UnlockPreview`, `ActionPreview`, `ActionDone`, `StepOutcome`; `NotFound`; `LinkBuilder`, `Scope`, `EntryKind`. `ui_page(templates=, static=)` (`UiPage.templates/static`), `Registry.link_builder`, `Registrations.link_builders()`, `Registrations.module_of(point, name)` |
| `src/steward/core/files/` | Read side for people (no writes): `browse.Browser(dbs, policy, shared=None)` (internal `BrowseScope` adds `shared`: `SharedArea(library_id, relpath, sharing, allowed)` = inside the item, `SharingRules.check(...).shareable`, snapshot relpaths),  (`libraries`, `locate(path, scope)` → `Located` or `NotFound`, `visible(lib, rel, scope)` = the scope rule, `locked_folders`, `listing`, `entry`, `by_kind` (search with empty query), `search`, `unlock_preview` (policy copy with the lock removed via `dataclasses.replace`, counts from `files`/`folders`), `open` → `OpenFile` (fd, entry, size, `pread`), `zip_members` → `ZipMember`s via `os.fwalk` without following symlinks; disk + `files`/`blobs` merge in `_known`); `zipstream.stream_zip(members)` (unseekable `ZipFile`, ZIP64, stored); `open_member` |
| `src/steward/api/share.py`, `src/steward/api/routes/shares.py` | Share links: `/s/{token}` and `/s/{token}/{rest:path}` (GET/POST, `@access("public")`; bad token or no provider → `not_found`; form read from the guard's cache; handler run in the threadpool; `NotFound` → the 404 page; `PUBLIC_HEADERS` no-store/no-referrer/noindex added). `ShareHandle` (the `ShareRequest`; `render` forces `principal=None`, `nav=[]`), `PublicItem` (the `SharedItem`: `Browser` in the `shared` scope, `Entry.path` made relative, no `file_id`), `_snapshot` (file ids → relpaths + their folders), `not_found` (`share_missing.html`) |
| `src/steward/api/web.py` | The core's `Web` implementation (also `state`, `page_url` from `app.state.page_paths`, `link_to`, `absolute_url` (`api.public_url` or the request's scheme/netloc), `WebFiles.privacy` via `SharingRules`): `make_web(request)` (finds the module page by longest mount-path prefix in `app.state.module_pages`), `WebHandle`, `WebFiles` (`check_scope`: locked scope → `is_recent` else `require_recent_auth`; also for `locked_folders`/`unlock_preview`), `WebActions` (`SensitiveActionError` → `require_recent_auth` → retry `confirmed=True`; `run_all` previews all, asks once), `file_response` (single `Range`, 206/416, inline only for `INLINE_TYPES`, `RAW_CSP` with `sandbox`, `PDF_CSP` + `X-Frame-Options: SAMEORIGIN` for PDFs), `disposition`, `first_link` (core pages → first `link_builder`) |
| `src/steward/modules/shares/` | Built-in module `shares` 0.1.0 (entry point, on by default; ADR 0020): `__init__.py` `SharesModule` (state migrations `migrations/state/0001_shares.py`, actions, page `shares.page` at `/ui/shares` with `badge=store.count_no_expiry`, `share_links(public.handle)`, job + `Every(15 min)` schedule `shares.expire` (lane `io`, `expire(ctx)`), exporter `store.export`); `store.py` (tables `shares_links`, `shares_snapshot`, `shares_access`; `Share` (`status(now)`, `active`, `ended`), `get`, `by_token`, `listing(past=, since=)`, `count_no_expiry`, `snapshot_ids`, `accesses`, `record` (counts all but `password_failed`), `failures`, `insert_share`, `change`/`end` (only while not ended), `expire_due`, `export` (no token/hash, `has_password`)); `credentials.py` (`new_token`, `new_share_id`, `suggest_password`, argon2 `hash_password`/`verify_password` (semaphore 2, `_hasher` patched in tests), `make_grant`/`check_grant` (HMAC keyed by the hash), `GRANT_COOKIE`, 12 h); `actions.py` (`shares.create` `CreateParams(path, expires_at, password, snapshot, share_id)`, `shares.update` `UpdateParams(share_id, expiry keep/set/never, expires_at, password_change keep/set/remove, new_password)`, `shares.revoke` `RevokeParams(share_id, reason revoked/undone)`; secrets in `Plan.transient`); `public.py` (`handle`: token → active share → password/grant → `open` → zip/raw/page; failures limit 10/address, 100/share per 15 min → 429; access log); `routes.py` (`/ui/shares` list (`status`, `q` on shown names, `age`), `GET|POST /new` (dialog; `created.html` shows link + password once), `GET /{id}`, `GET|POST /{id}/edit`, `POST /{id}/revoke`, `POST /undo`; hidden items → `name=None`); templates `list`, `new`, `created`, `detail`, `edit`, `errors`, `_row_item`, `_link`, `public/password|file|folder`; static `shares.css`, `shares.js` (copy buttons) |
| `src/steward/modules/files_ui/` | Built-in module `files_ui` 0.3.0 (Share links via `web.page_url("shares.page")` in the normal scope only) (entry point in `pyproject.toml`, on by default): `__init__.py` `FilesUiModule` (pages `files_ui.page` at `/ui/files` and `files_ui.locked` at `/ui/locked` "Locked Folder", same `templates/` + `static/`, link builder `files_ui.links`); `routes.py` `make_router(scope)` → `router` (normal) and `locked_router` (router dependency `_gate`: `recently_authenticated()` else `require_reauth()`): `GET ""` (`?path=` folder, `?kind=` by kind, no path → libraries / the only library (normal) or `locked.html` list of `locked_folders()` (locked); `did`/`n`/`undo` → notice + Undo), `GET /view`, `GET /raw` (`inline=1`), `GET /zip`, `POST /mkdir`, `GET|POST /rename`, `GET|POST /move` (picker `to=`, `to_scope=` honoured only in the locked router; moving out needs `confirm_out` else 409), `GET|POST /delete`, `GET|POST /lock` (normal: lock, `permanent` optional; locked: only make permanent), `POST /undo`; locked only: `GET /search?q=`, `GET|POST /unlock` (preview page, then `folder.unlock`, redirect to `/ui/files`). Templates get `base`/`scope`: `browse`, `kind`, `libraries`, `locked`, `search`, `lock`, `unlock`, `view`, `rename`, `move`, `delete`, `errors` + partials `_nav`, `_crumbs`, `_undo`, `_back`, `_found` |
| `src/steward/core/executor/uploads.py` | `Uploads(dbs, executor, UploadsConfig, clock=)` (`Runtime.uploads`): `start(folder, name, size, actor=, confirmed=)` (checks; creates `UPLOAD_PREFIX + id` exclusively; `SensitiveActionError` for locked folders unless `confirmed`), `get`, `list_open(actor=)`, `write(id, offset, data, actor=)` (per-upload `threading.Lock`; offset must equal `received`; truncates a longer temp file; `pwrite` + `fsync`), `finish(id, actor=, name=)` (runs `file.upload` with `confirmed or info.confirmed`), `cancel`, `expire()`; `UploadInfo` (`path`, `temp`, `as_dict`), errors `UploadNotFound`, `UploadConflict(suggestion)`, `OffsetMismatch(offset)` (all `ActionRefused`); `FREE_SPACE_MARGIN` 64 MiB |
| `src/steward/api/routes/uploads.py` | `/api/v1/uploads` (default `session` level): `POST ""` (`StartBody` folder/name/size/scope; `Browser.locate` for the scope; locked scope → `is_recent` or `require_recent_auth`, then `confirmed=True`), `GET ""` (`?scope=`; filtered by `Browser.visible`), `GET /{id}`, `PUT /{id}` (async; 415 unless `application/offset+octet-stream`/`octet-stream`, 400 without `Upload-Offset`, 413 over `chunk_bytes`, `ClientDisconnect` → 400 with nothing written; write in the threadpool), `POST /{id}/finish` (`FinishBody` name), `DELETE /{id}` (204); `_refusal` maps errors to 409 (+`suggestion`/`offset`), 404, 403, 400, 500 |
| `src/steward/modules/files_ui/static/upload.js` | Browser client: queue, one file at a time; resume via `localStorage` (`steward.uploads.v1`, key folder+name+size+lastModified) or the server's open list; backoff up to 30 s, 40 failures; keep-both/skip buttons on 409; Cancel; lists unfinished uploads of the folder at load; reloads when all succeeded. Shown only on writable folders (`#uploads` section, `data-folder`, `data-scope`, `data-csrf`) |
| `tests/integration/test_uploads.py` | Uploads: pieces + finish + undo, offset mismatch and crash truncation, collisions ("keep both"), bad destinations, symlinked folders, locked folders (confirmed start, 0600), free space and size limits, cancel/expire, ownership, discovery ignores temps, `file.upload` refusals; HTTP flow with a dropped piece, refusals (413/415/400/409/404), scopes, CSRF/tokens, the page's upload panel, re-auth window 0 |
| `tests/locked_support.py`, `tests/integration/test_locked_folder.py`, `tests/privacy/test_locked_routes.py` | `LockedEnv` (loads `files_ui` and `shares`; library with `private` type folder, locked `vault` with its own locked `vault/own`, `SECRET_NAME`/`SECRET_CONTENT` markers, `forget_reauth()`); the Locked Folder end to end (gate on every route, window 0, root list without trash, full file manager, move out with confirmation, search/kinds, lock + undo, permanent locks from UI/API/executor/settings refused, host unlock, unlock preview, type-locked folders); the route walk (its env also has a share whose item is in a locked folder, and requests to its `/s/` link on every share route): every route × locked paths in every parameter, non-recent session and API token leak nothing, recent session only via `SHOWS_LOCKED` |
| `tests/integration/test_shares.py` | `ShareEnv` (`LockedEnv` + excluded `*.key`, local-only override, locked `docs/closed`, custom non-shareable type, trash file; `create()`): every refusal rule, parameters, no-expiry sensitivity, no secrets in the journal, password pages + grants + rate limit, folder listings/zips leaving out unshareable things and traversal, snapshots, locking after sharing (404 + redacted owner pages), identical 404s, the expiry job, undo/revoke, update undo, the dialog, re-auth for no expiry + nav badge, Past, editing, no share links in the Locked Folder, exporter, module off, two providers |
| `tests/integration/test_files_ui.py` | The file manager end to end (listing rules, symlinks, 404 for locked/hidden/bad paths, inline vs attachment + CSP, ranges, zips, mkdir/rename/move/delete + undo, re-auth on delete with window 0, search links, module off) |
| `src/steward/cli.py` | Typer app, global `--config-dir`: `version`, `modules`, `api` (`steward.api.serve`), `config check`, `db upgrade`, `db status`, `jobs list`, `jobs retry <id>`, `worker`, `folders import|list|show|map|unmap|lock|unlock|role|move`, `types list|show|rename|delete`, `scan [lib[:path]…] [--allow-empty]`, `files list [lib:path] [--missing]`, `stages list`, `stages plan`, `user list|reset-password [NAME]|unlock [--username|--ip]|sessions [--all]|logout [ID]|settings [--reauth/--no-reauth] [--reauth-minutes N]`, `tokens list [--all]|create LABEL [--scope …] [--expires-days N]|revoke ID`, `files sensitivity LIB:PATH local-only|excluded|clear`, `search QUERY [--library --folder --kind --limit]`, `journal list [--limit]|undo ID` (host is trusted: `confirmed=True`), `backup [--keep N]`, `export [--output F]`; `db upgrade` backs up first via `before_migrate` (actor `cli`); `worker` also queues a startup `core.reconcile`, a full stage pass if the graph signature changed, and runs the `Watcher` thread |
| `config/*.example.yaml` | Annotated examples of `steward.yaml` (incl. `backup:`), `modules.yaml`, `pipeline.yaml`, `libraries.yaml`, `folders.yaml`, `privacy.yaml`, `llm.yaml` (tests validate and import them) |
| `examples/hello-module/` | Out-of-tree module `hello` 0.3.0 (dist `steward-hello`): config `greeting`; registers health check `hello`, stage `hello.lines` (text files ≤10 MiB → `hello_line_counts` in `index.db`, migration `migrations/index/0001`), subscriber `hello.greet` (`file.discovered`, logs), UI page `hello.page` at `/ui/hello`. Dev dependency only; not in the image |
| `tests/unit/test_auth_primitives.py`, `tests/integration/test_auth.py`, `test_auth_cli.py` | Password hashing/rules, secrets/CSRF, client IP, settings bounds; the `Auth` service end to end (sessions, expiry, lockouts, re-auth windows incl. 0 and off, tokens can't do session things, password change/reset, prune, health); CLI. Integration tests patch `passwords._hasher` with cheap parameters (fixture `fast_hashes`) |
| `tests/support.py` | Helpers (importable as `support`): `FakeEntryPoint`, `make_module(name, register=...)`, `eps`, `write_config`, `make_runtime(tmp_path, modules, upgrade=True, **files)`, `use_fast_hashes(monkeypatch)` (core and `shares` argon2) |
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
| `docs/adr/` | ADRs 0001–0019, index, template |
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
  nav=, templates=, static=, badge=)`, `link_builder`, `action`, `exporter`,
  `share_links(handler)`. Point keys in `Registrations`: `health_check`, `job`,
  `subscriber`, `schedule`, `stage`, `migrations:state`, `migrations:index`, `ui_page`,
  `link_builder`, `action`, `exporter`, `share_links`.
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
  copy-holding role folders and each library's `.steward` (outermost refs only), in SQL. Journal entries touching a
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

### Module web services and the file manager (implemented; ADR 0017)
- Built-in modules (`src/steward/modules/`) obey the same import contract as
  out-of-tree ones: only `steward.sdk`. Everything they need from the core comes
  through `WebDep` (per request) or `Registry`.
- The scope rule is `Browser.visible`: `normal` = not `Visibility.is_hidden` (locked,
  `visible: false`, trash type, copy-holding roles, `.steward`) and not a
  `.steward-tmp-*` name; `locked` = `Visibility.is_locked`. Invisible → `NotFound` → 404
  (HTML or JSON by path prefix). `WebFiles.check_scope("locked")` passes if `is_recent`,
  else calls `require_recent_auth` (→ `ReauthRequired` → `/reauth?next=` redirect).
- Paths in the file manager are `library:relpath` exactly as on disk (`split_path` +
  `check_segments`, no NFC normalization). Reads use `open_dir` (`O_NOFOLLOW` per
  component); `FolderMissing` is the public alias of guarded's `_Missing`.
- Module templates are addressed as `<module>/<name>` (`WebHandle.render` adds the
  prefix and `static_url`). Partials inside a module template are included by full
  name (`{% include "files_ui/_nav.html" %}`).
- Messages after redirects in `files_ui` come from its fixed `DONE` map
  (`?did=<key>&n=<count>`); `undo=<ids>` only feeds the Undo form (undo by ID is
  already allowed from `/ui/journal`).
- Bulk changes use `Actions.run_all`: preview every step, one `require_recent_auth`
  if any is sensitive, then run each with `confirmed=True`; per-step errors are listed
  on `errors.html` with an "Undo what was done" button.
- The security suite (`test_api_security.py`) loads `FilesUiModule`, so its routes are
  checked for anonymous refusal, CSRF and token refusal like core routes.
- Tests that need the module pass it explicitly (`make_runtime(tmp_path,
  (FilesUiModule,))`); test runtimes only load modules given as fake entry points, so
  `files_ui` is absent elsewhere. Real installs find it through its entry point
  (`uv sync` after changing entry points).

### Uploads (implemented; ADR 0018)
- State revision `0006`: `uploads` (not exported: transient). `config.steward.uploads`
  (`UploadsConfig`: `chunk_mib` 8 (1–64), `max_file_gib`, `stale_hours` 24, `max_open` 100).
- Temp file `UPLOAD_PREFIX` (`.steward-tmp-up-`) + 32 hex chars in the destination
  folder; `TEMP_PREFIX` names are hidden by `Browser.visible` and skipped by discovery.
- `file.upload` (`UploadParams` temp/path/size): temp must match `UPLOAD_TEMP` and sit
  in the same folder; preconditions temp = file of `size`, path absent; apply =
  `fs.move`; post-state without content hash; inverse = `file.delete` plan (so undoing
  an upload is sensitive, like undoing `file.create`). In `NO_EXAMPLES` of the safety
  harness.
- Job `core.uploads.expire` (lane `executor`, `Every(1h)`) registered by
  `builtin.register_uploads` after modules (closure over the runtime).
- The guard parses forms only for urlencoded/multipart, so raw `PUT` bodies reach the
  route untouched; CSRF comes from `X-CSRF-Token`.
- Manual browser runs: `~/steward-sandbox`-style instance + Playwright (see the
  handoff); not part of `make check`.

### Locked Folder (implemented; ADR 0019)
- `/ui/locked` = `files_ui.locked`, built from the same `make_router` as `/ui/files`;
  every route behind `_gate`. Templates must link through `{{ base }}` (never a
  hard-coded `/ui/files`), and `_undo`/`errors` use `undo_base or base`.
- Locked scope rule: `Visibility.in_locked_view` (locked and not `concealed`), so the
  locked trash (`trash/locked`) and other hidden folders inside never show there or in
  the locked search; root = `Visibility.locked_roots()`.
- `folder.lock` permanent: `Plan.sensitive=True`, `Applied.result["permanent"]`, inverse
  `None`; plan refuses "already locked" unless making an own non-permanent lock
  permanent. `folder.unlock` plan refuses inherited (`lock_from != path`) and permanent
  locks; `folder_store.unlock(allow_permanent=True)` (CLI) is the only way out.
- Moving out: locked router only, `to_scope=normal` + `confirm_out`; redirects to
  `/ui/files?...did=move_out`. Normal router ignores `to_scope` (always normal).
- Unlock redirects to `/ui/files?path=<folder>&did=unlock&undo=<id>` (undo = lock again).
- Jinja `urlencode` encodes `:` as `%3A`: test hrefs as `path=lib%3A...`.
- The privacy walk builds params with every name routes read (`path`, `folder`, `to`,
  `q`, `back`, `entries`, `scope`, `to_scope`, `locked`, …); never put a secret marker
  in a request parameter (re-auth redirects echo the URL in `next=`). New routes are
  walked automatically; add to `SHOWS_LOCKED` only for a route meant to show locked
  content after a recent check.

### Share links (implemented; ADR 0020)
- Public routes: only `/s/{token}` and `/s/{token}/{rest:path}` (GET, POST) besides
  login/setup/health; listed in `PUBLIC` of `test_api_security.py`. Guard rules apply
  (public forms: pre-session CSRF; a logged-in owner: session CSRF).
- The core enforces the item on every request (`ShareHandle.open`): exists as `kind`,
  not a library root, `SharingRules.check(...).shareable`; then `Browser` in the
  `shared` scope (inside the item, each child checked again, snapshot relpaths). Never
  trust the provider for what may be served.
- Share actions change only `shares_*` rows via `GuardedFS.write_state`; ops re-check
  "not ended" and raise `ActionRefused` (rolls back the journal entry). `reads=(path,)`
  so the journal redacts entries about items in locked folders (and touching a locked
  item makes revoke/update sensitive). Token made in `apply`; password and snapshot ids
  in `Plan.transient`; journal params carry `password: bool`.
- `shares.create` undo = `shares.revoke` with `reason: undone`; revoke has no inverse;
  `shares.update` inverse only for expiry changes without a password change.
- Expiry: `Share.status(now)` treats `expires_at <= now` as expired immediately; the
  job only records `ended_at = expires_at`.
- Owner pages never show the names of items hidden in normal views (`Files.privacy`
  → `hidden`), and `q` filters only shown names.
- Tests: `use_fast_hashes` patches `credentials._hasher`; `LockedEnv` loads `shares`.
  Tests that run the CLI against a `make_runtime` data dir must load the installed
  built-in modules (`FilesUiModule`, `SharesModule`) so their migrations exist.
- Manual browser runs: a sandbox instance + Playwright (see the handoff); not part of
  `make check`.

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
2. File manager, Locked Folder, built-in shares *(in progress: chunks 1–4 of 5 done — module web SDK + `files_ui`, resumable uploads, Locked Folder, share links)*.
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
