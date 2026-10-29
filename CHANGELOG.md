# Changelog

All notable changes to steward-ai are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Before 1.0, any release may change the SDK.

## [Unreleased]

### Added
- Phase 2 acceptance tests (`tests/acceptance/`): each criterion end to end over HTTP,
  starting from a fresh install finished through the setup wizard, on two folder
  layouts: an upload resumed after a dropped connection and a server restart; locked
  folders hidden from every normal page, search, zip and API read without a fresh
  password, and uploads into the Locked Folder; a permanent lock surviving every way to
  remove it except the host CLI; no-expiry shares (warning, password, listing) and
  revoked shares (Past, every link URL gone). The 5 GB browser upload was checked by
  hand against the container image.
- Share links (`shares`, a built-in module on by default; ADR 0020): share a file or a
  folder from the file manager as a public link `/s/<token>` (128-bit token) with an
  expiry (7 days by default, 1/7/30 days, any number of days, or none: allowed with a
  warning and a password check) and a password (on by default, with a suggested one;
  shown once). Folder shares can be a snapshot of the files there now. Visitors get a
  password form (wrong passwords are slowed down), file pages with previews and
  downloads, folder listings and zips. The core re-checks every request: nothing locked,
  hidden, `local-only`, `excluded` or in a non-shareable folder is ever served, even if
  it changed after sharing. `/ui/shares` lists active and past shares (filters, access
  count, last access, an access log per share, change expiry or password, revoke) and
  hides the names of items now in locked folders; the navigation shows how many shares
  never expire. Actions `shares.create|update|revoke` are journaled (undo a share =
  turn it off; revoking is final), job `shares.expire` every 15 minutes, exporter
  without tokens and password hashes. `api.public_url` sets the links' address.
- SDK: share links (`steward.sdk.shares`: `Registry.share_links`, `ShareRequest`,
  `SharedItem`, `SharedListing`, `TOKEN_PATTERN`); module state access
  (`steward.sdk.state`: `StateAccess`, `UTCDateTime`, `utcnow`; `Web.state`,
  `JobContext.state`, `ReadOnlyFS.read_state`, `GuardedFS.write_state`); privacy
  classes (`PathPrivacy`, `ReadOnlyFS.privacy`, `Files.privacy`);
  `ReadOnlyFS.file_ids_in`, `GuardedFS.actor`, `Plan.transient` (never journaled);
  `Web.page_url`, `Web.link_to`, `Web.absolute_url`; `ui_page(badge=)`.
- The Locked Folder (`/ui/locked`, ADR 0019): behind a password check (subject to the
  re-auth window), the locked folders and the full file manager inside them (uploads,
  new folder, rename, move, delete to the locked trash, previews, downloads, zips),
  files by kind and search inside. Lock a folder from Files ("Lock folder…", optionally
  permanent); unlock from the Locked Folder after a preview of what becomes visible
  (password again); make a lock permanent there. Moving items out of the Locked Folder
  asks for a confirmation and the password. A privacy test walks every route with
  locked paths.
- SDK: `Files.search`, `Files.locked_folders`, `Files.unlock_preview`, `by_kind(scope=)`,
  `FilePage` (`KindPage` stays as an alias), `LockedFolder`, `UnlockPreview`,
  `FolderListing.lock_from`, `PathPolicy.lock_from`/`permanent`, `Plan.sensitive`,
  `GuardedFS.set_lock(permanent=)`.
- Resumable uploads (ADR 0018): drag-and-drop or choose files in the file manager; large
  files go up in pieces, survive dropped connections and server restarts, and resume
  when the same file is chosen again (even after closing the browser); a name that's
  taken offers "keep both". `/api/v1/uploads` (start, `PUT` pieces with
  `Upload-Offset`, finish, status, list, cancel), `file.upload` action (undo = trash),
  state revision `0006` (`uploads`), `uploads:` in `steward.yaml`, hourly
  `core.uploads.expire`.
- File manager (`files_ui`, the first built-in module, on by default) at `/ui/files`:
  browse by folder or by kind, previews (images, PDF, text, audio, video), downloads
  with byte ranges, folders and selections as streamed zips, new folder, rename, move
  (folder picker), delete to the trash (asks for the password), Undo right after each
  change. Search results link into it.
- SDK web services for module pages (`steward.sdk.web`, ADR 0017): `WebDep` /
  `Web` (render module templates in the core layout, re-auth), `Files` (scoped
  listings and reads, `normal` or `locked`), `Actions` (run, run_all, undo through the
  executor); `ui_page(templates=, static=)`, `link_builder`, `NotFound`; `ActionFailed`
  is now part of the SDK.
- Configuration: `steward.yaml`, `modules.yaml`, `pipeline.yaml` (all optional, unknown
  keys rejected, `config_version`), annotated examples in `config/`.
- Databases: `state.db` and `index.db` (SQLite WAL), per-owner Alembic migrations with
  their own version tables; module tables must be named `<module>_*`.
- Job queue with lanes, priorities, leases, retries with backoff, dead jobs, dedupe keys.
- Event outbox with per-subscriber cursors and at-least-once delivery jobs.
- Scheduler (`Every`, `Daily`), with per-schedule timing overrides in `pipeline.yaml`;
  core maintenance schedules prune finished jobs and old events.
- `steward worker` (threads per lane, graceful stop on SIGTERM), `steward config check`,
  `steward db upgrade|status`, `steward jobs list|retry`, global `--config-dir`.
- SDK extension points: `job`, `subscribe`, `schedule`, `migrations`; `Event`,
  `JobContext`, `PermanentJobError`, `Every`, `Daily`.
- JSON logging. ADRs 0007 (per-owner migrations) and 0008 (jobs, events, scheduler).
- Libraries, folder identities (follow renames, `path_history`), 13 built-in folder
  types plus custom types with `extends`, folder mappings, locks (including permanent
  locks), and roles (`trash`, `edit_backups`, `inbox`, `digest_output`) with hidden
  defaults for trash and edit backups.
- Effective folder settings where restrictions always carry down into subfolders
  (ADR 0009).
- `libraries.yaml` and `folders.yaml` seed files, `steward folders …` and
  `steward types …` commands.
- Discovery (ADR 0010): full scans on start and every 6 hours plus an inotify
  watcher; BLAKE3 full or sampled hashes per folder type; MIME detection; files keep
  their identity through edits, renames and moves (also across libraries), and renamed
  folders keep their type, lock and roles. Idle IO priority, optional read-rate cap,
  temp-file patterns, refusal to treat an empty (unmounted) library as deleted.
  `steward scan` and `steward files list`. New dependencies: `blake3`, `watchdog`.
- Stage framework (ADR 0011): `Registry.stage(...)`, `StageContext` (read-only file
  access, `index.db` transactions), `StageResult`, `FileChanged`; a stage graph checked
  at startup (no duplicate producers, missing inputs or cycles); a planner that queues
  `core.stage.<name>` jobs per unique content, honours folder types (`stages`
  add/remove, `index` policy) and re-runs stages after a version bump or recomputed
  input. Discovery's hashing is the root stage `core.hash` (stage runs recorded as
  `hash` before are not migrated; rebuild the index). `steward stages list|plan`.
- The example `hello` module now also registers a stage (`hello.lines`, line counts in
  its own `hello_line_counts` table) and an event subscriber.
- Accounts and login (ADR 0012): one owner account with an argon2id password (length,
  variety and common-password rules), server-side sessions with idle and maximum
  lifetimes, a re-auth window for sensitive steps (default 15 minutes, `0` = every
  time, can be turned off), scoped API tokens (`read`, `propose`; never approval or
  locked folders), CSRF tokens derived from sessions, login rate limiting with
  temporary lockouts per address and per username, client addresses through
  `trusted_proxies`. Runtime settings store (`settings_kv`). `steward user
  list|reset-password|unlock|sessions|logout|settings` and `steward tokens
  list|create|revoke`. Health check `core.auth`, daily `core.auth.prune`. New
  dependency: `argon2-cffi`.
- `steward api` (ADR 0013): migrates the databases on start, runs the executor lane,
  serves the web UI on port 8420 (`api:` in `steward.yaml`). Login, logout and re-auth
  pages; session cookies (`HttpOnly`, `SameSite=Strict`, `Secure` over HTTPS); CSRF
  checks on every state-changing request and refusal of cross-site ones; bearer API
  tokens; 429 with `Retry-After` during lockouts; strict security headers. Every route
  needs a session or token except `/health`, `/login` and setup. `/api/v1/me`.
- First-run setup wizard (`/setup`): a one-time setup token printed in the log, owner
  account, libraries (suggested from `libraries.yaml` and `setup.library_dirs`, or
  imported from the config files), suggested folder types for top-level folders, a
  private folder (existing or new, optionally locked permanently) or a warned skip,
  trash and edit-backup folders. Resumes where it was left; only ever adds settings.
- Settings page (`/ui/settings`): change password, list and log out sessions, re-auth
  window (with a warning when off), create and revoke API tokens.
- SDK: `Registry.ui_page(...)` and `UiPage`: modules add web pages, always behind login
  and CSRF checks. The `hello` example module (0.3.0) adds `/ui/hello`.
- A library added while the worker runs is scanned right away.
- New dependencies: FastAPI, Uvicorn, Jinja2, python-multipart.
- Privacy (ADR 0015): `privacy.yaml` (secret patterns, path and name rules), per-file
  overrides (`steward files sensitivity`), unknown files treated as local-only and
  locked. Excluded files are never hashed or given stages.
- Model gateway: `llm.yaml` (endpoints with required `trust`, roles, `local_fallback`),
  an OpenAI-compatible client using only the standard library, and `Gateway.call`, which
  refuses excluded content, locked files in chat and local-only content to remote
  endpoints, with per-endpoint concurrency limits and content-free logs. Remote
  endpoints are refused at startup for now. A privacy test suite.
- Executor (ADR 0014): `ActionType` SDK (`Registry.action`), a guarded filesystem
  (no symlinks, no overwrites, checked names), the journal and undo, core actions
  `file.mkdir|mkdirs|rmdir|move|rename|delete|create|edit` and `folder.lock|unlock`,
  trash per library with a locked subfolder for files from locked folders, edit
  backups, and an executor-safety test harness run over every action. The setup wizard
  creates the private folder through the executor. `steward journal list|undo`.
- Keyword search over file names and paths with kind, size, date, library and folder
  filters; locked, hidden and trash folders are never searched outside the Locked
  Folder scope. `steward search`, `/ui/search`.
- `/api/v1`: libraries, folders, files, search, actions (preview, run), journal (undo),
  jobs (retry), health and `openapi.json`. Tokens with `read` can look things up;
  changing anything needs a session.
- UI pages: `/ui/journal` (undo), `/ui/jobs` (retry), `/ui/health` (checks, failed
  logins, modules) and `/ui/settings/folders` (types, locks, roles; unlocking or
  lowering privacy asks for the password).
- Backups (ADR 0016): `steward backup`, `steward export`, a nightly `core.backup`
  schedule and health check, and an automatic backup before migrating existing
  databases. `backup:` in `steward.yaml`; `Registry.exporter` for modules.

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

### Changed
- File manager: the message after a change ("Folder created." and so on) and its Undo
  button are now one bar, instead of the message at the top of the page and the button
  on a separate row below.
- `JobContext` has a new required field `state` (built by the worker).
- `files_ui` 0.3.0: "Share" and "Share folder…" links (when the `shares` page exists; never in
  the Locked Folder).
- `folder.lock` takes `permanent` (sensitive; can't be undone) and can make an existing
  lock permanent; `folder.unlock` refuses permanent locks and locks set on a parent
  folder already when planning. The locked search scope leaves out locked trash and
  other hidden folders inside locked folders. `files_ui` 0.2.0.
- The page Content-Security-Policy also allows same-origin media, frames (PDF
  previews) and `fetch`; `base.html` has a `head` block for module stylesheets.
- `deploy/compose.example.yaml` runs `steward api` with a health check; the worker
  starts once the API is healthy (databases migrated).
- Registered names must start with the module's name (`<module>.<name>`); `core` and
  `steward` are reserved module names.
- Separately installed modules are off until enabled in `modules.yaml`; built-in
  feature modules are on by default. `steward modules` shows enabled/disabled.
