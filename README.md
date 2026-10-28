# steward-ai

> **Status: pre-alpha, design stage. There is no runnable code yet.** This README
> describes what steward-ai is being built to do. Sections marked *(planned)* aren't
> implemented.

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
cp deploy/compose.example.yaml compose.yaml   # choose folders to mount, data dir, port
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

### License
Licensed under the [Apache License 2.0](LICENSE).

---

## For Claude agents

Detailed orientation so you don't have to scan the repository. Keep this section
accurate whenever the code changes. Local, untracked files may also exist for you:
`CLAUDE.md` (rules) and `claude-handoff.md` (user instructions, current state, next
steps, log). Read them first if present. A maintainer-only design document
(`plan.md`, untracked) is the authoritative design if present.

### Current status
- No code yet. Next milestone: **Phase 0**, the repository skeleton (see "Roadmap").
- Nothing in "Architecture" below exists yet; it's the agreed design to implement.

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
- Modules import only `steward.sdk` and themselves (import-linter). Only `core.llm`
  imports the `openai` SDK. Only `core.executor` writes library files.
- No code refers to a folder by name or position; read behavior from folder types.
- Module tables are prefixed `<module>_` with their own Alembic branch; irreplaceable
  module data registers an `Exporter`.
- Logs never contain file content.
- Tests never call real models by default (`-m live` opts in); a fake LLM adapter is used.
- No personal or deployment-specific details in tracked files; use placeholders.
- Update this README (both sections) whenever the code changes.

### Planned repository layout
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
Python 3.12+, uv, FastAPI, Pydantic v2, SQLAlchemy 2 Core + Alembic, SQLite (WAL) with
FTS5 and sqlite-vec, `openai` SDK against OpenAI-compatible endpoints, watchdog,
BLAKE3, pypdf + docling, ocrmypdf + Tesseract, exiftool, argon2-cffi, Jinja2 + htmx,
Typer, pytest + hypothesis, ruff, mypy, import-linter.

### Roadmap
0. Foundation: repo skeleton, SDK `Module`/`Registry`, module loader, ADRs, license.
1. Core: config, DBs, queue, events, libraries/folder types, discovery, auth, setup
   wizard, privacy gateway, executor + journal, keyword search, backup/export.
2. File manager, Locked Folder, built-in shares.
3. Indexing and tagging (documents, search, fields, tags, photos).
4. Chat and AI actions (agent, built-in chat, proposals, triage, `openai_api`, `openwebui`).
5. Integrations (`immich`).
6. Background intelligence (`tag_gen`, `dedupe`, `digest`).
7. Later modules (`remote_llm`, file-manager integrations, `mcp`, `notify`, SSO, 2FA, …).

### Commands
None yet. This section will list setup, `make check`, test and run commands once Phase 0 lands.
