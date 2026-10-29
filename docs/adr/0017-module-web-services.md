# 0017. Web services for module pages, and the file manager as a module

- Status: Accepted
- Date: 2026-10-28

## Context
Phase 2 adds the first built-in modules: the file manager (`files_ui`) and share links
(`shares`). Built-in modules follow the same import contract as anyone else's
(ADR 0003): they may import `steward.sdk` and nothing else from steward. A file manager
needs much more than a router, though: the core's page layout, the logged-in owner, folder
listings that respect locked and hidden folders, safe file reads, and a way to change
files that goes through the executor (ADR 0005) with re-auth for sensitive steps. Copying
those rules into each module would be the way they drift apart.

## Decision
- **A per-request handle.** `steward.sdk.web` defines `Web` (and `Files`, `Actions`) as
  protocols plus a FastAPI dependency, `WebDep`. The core implements them in
  `steward.api.web` and installs the factory on the app (`app.state.steward_web`); the
  handle knows which module's page the request is for by its mount path.
- **Rendering.** `ui_page(..., templates=dir)` registers a module's Jinja2 templates;
  they're loaded as `<module>/<name>` next to the core's, so they can extend
  `base.html`. `Web.render` fills the same context the core's pages get (CSRF token,
  navigation, the logged-in user). `ui_page(..., static=dir)` is served publicly at
  `/static/m/<module>/` (scripts and styles only; the startup route check allows exactly
  these mounts besides `/static`).
- **Reading.** `Files` (implemented by `steward.core.files.Browser`) answers in a
  *scope*: `normal` never reaches locked, hidden (`visible: false`, trash, copy-holding
  roles, `.steward`) folders or temporary upload files, and answers `NotFound` (404)
  instead; `locked` reaches only locked folders and needs a recent password check.
  Listings come from the disk, merged with `state.db`/`index.db` for IDs and types, so
  files discovery hasn't seen yet still show. Paths are opened one component at a time
  with `O_NOFOLLOW`; symlinks and special files are never listed, followed or served.
- **Serving content.** `file_response` streams with single byte ranges (media players
  need them). Only images (not SVG), audio, video, PDF and plain text are shown inline;
  everything else downloads. Content gets its own Content-Security-Policy with
  `sandbox` (PDFs can't be sandboxed in browsers' viewers, so they get
  `default-src 'none'` and may only be framed by our own pages). The page CSP gained
  `media-src 'self'`, `frame-src 'self'` and `connect-src 'self'`.
- **Zips** are built while they download (`core.files.zipstream`): no temporary file,
  ZIP64, stored without compression, folders hidden in the scope left out.
- **Changing.** `Actions.run`/`run_all`/`undo` call the executor as the logged-in owner.
  A sensitive action (or one in a locked folder) asks for the password through the
  core's re-auth redirect; `run_all` asks once for a whole selection. Module code never
  sees `SensitiveActionError`.
- **Linking.** `Registry.link_builder(name, fn)` lets a module say how to link to a file
  or folder; the core's pages (search results) use the first one. Without `files_ui`,
  search results are plain text again.
- `files_ui` is a built-in module (entry point in the `steward-ai` distribution, kind
  `feature`, so on by default). It uses only plain forms: selections are checkboxes,
  every change is a POST, and each change redirects back with an Undo button for the
  journal entries it made.

## Consequences
- Other modules (shares, chat, tags) get the layout, scopes and action handling for
  free, and the security suite covers their routes the same way.
- The SDK now names FastAPI's `Request`/`Response` types at runtime and is a bigger
  surface to keep stable; `Web` is a protocol, so tests can fake it.
- The scope rules live in one place (`Browser.visible`), which the Locked Folder (Phase
  2, chunk 3) and shares (chunk 4) build on.
- `ActionFailed` moved to the SDK (the core re-exports it).
