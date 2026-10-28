# 0013. The web API shell, its access checks, and the setup wizard

- Status: Accepted
- Date: 2026-10-28

## Context
`steward api` serves the web UI, login, the setup wizard and `/api/v1`, and later the
file manager and share links. Principle 6 says it must be safe with no proxy in front:
every route refuses a request without a session or token, except setup and share links
(plan §17, "Security"). Modules add UI pages (plan §5.2), so the checks have to apply to
code the core doesn't control. On first start nobody owns the instance yet, and anyone
on the network could otherwise claim it (plan §6.4).

## Decision
- **One guard for every route.** `steward.api.security.guard` is a dependency of the
  whole FastAPI app, so it runs for core routes and module pages alike. It
  authenticates (an `Authorization: Bearer` header if present, which never falls back to
  the cookie, else the session cookie) and applies the route's access level:
  `public` (`/health`, `/login`, the setup-token form), `setup` (only while no owner
  exists and with the setup token's cookie), `session` (a logged-in browser) or
  `principal` (session or API token; `/api/v1`). **`session` is the default.** Core
  routes mark their level with `@access(...)`, which records the function in a table in
  `steward.api`; module code can't import that package (import contract), so module
  pages are always `session`. Setting attributes on a function changes nothing.
- **Fail closed at startup.** `create_app` refuses any route the guard wouldn't run for
  (anything but FastAPI `APIRoute`s and the `/static` mount), module pages on core paths
  (`/ui/settings`, `/ui/jobs`, `/ui/health`, `/ui/proposals`, `/ui/journal`) and two
  modules on one path. FastAPI's own docs and OpenAPI routes are off (they would bypass
  the guard); `/api/v1` will publish its OpenAPI description through a guarded route.
- **CSRF.** Every POST/PUT/PATCH/DELETE from a session carries the session's CSRF token
  (an HMAC of the session secret, ADR 0012) as form field `csrf_token` or header
  `X-CSRF-Token`. Forms without a session (login, setup) use a double-submit cookie
  (`steward_presession`, random, `SameSite=Strict`). Requests with
  `Sec-Fetch-Site: cross-site` that change anything are refused outright. Bearer tokens
  need no CSRF token (browsers never send them on their own).
- **Cookies:** `steward_session` (`HttpOnly`, `SameSite=Strict`, `Secure` when the
  request came over HTTPS directly or from a trusted proxy with `X-Forwarded-Proto:
  https`; `api.secure_cookies: always|never` overrides), lifetime `session_max_days`.
- **Re-auth** is a redirect: a sensitive step that raises `ReauthRequired` sends the
  browser to `/reauth?next=<page>` (the page itself for GETs, the referring page for
  forms) and back. Lockouts answer 429 with `Retry-After`. `?next=` only accepts
  same-site paths.
- **Responses** carry a strict Content-Security-Policy (no inline scripts or styles, no
  framing), `nosniff`, `Referrer-Policy: same-origin` and `Cache-Control: no-store`
  (except static files). Pages are server-rendered Jinja2 with no JavaScript yet; htmx
  arrives with the first page that needs it.
- **Setup token.** While no owner exists, `steward api` makes a random token, keeps it in
  memory only and prints it in the log (`setup token: …`). `/setup` takes it and sets a
  one-hour cookie that opens `/setup/owner`. Creating the owner logs them in and closes
  the token steps for good. A restart prints a new token.
- **Wizard steps** after the owner (libraries, folder types, private folder, trash and
  backups) need that session. Progress is stored in `settings_kv` (`setup.step`,
  `setup.finished`, `setup.private_folder`, `setup.private_skipped`), so an interrupted
  setup resumes after login, and the home page sends the owner back until it's finished.
  The wizard **only adds**: it never changes a folder that already has a type (for
  example from `folders.yaml`), so it can't loosen anything. Type suggestions come from
  folder names (number prefixes removed), marker files (`.git`, `.obsidian`) and the
  kinds of up to 2000 entries inside; they're suggestions, the owner accepts each one.
  Library suggestions are the entries of `libraries.yaml` and the folders inside
  `setup.library_dirs` (default `/libraries`, where the example compose file mounts
  them); overlapping library roots are refused.
- **Creating the private folder** is the first library write. It goes through
  `steward.core.executor.create_folder` (each path component opened with `O_NOFOLLOW`
  relative to its parent, mode 0700, under the process-wide `WRITE_LOCK`), the seed of
  the executor; it isn't journaled yet.
- **Processes.** `steward api` migrates both databases on start (core, then modules),
  prints the setup token if needed, runs the `executor` lane in a thread (no
  housekeeping: dispatch and schedules stay in the worker) and serves with Uvicorn
  (`proxy_headers` off: client addresses come from `trusted_proxies`, ADR 0012; no
  access log). `/health` is public and says only `{"status": "ok"}`.
- **Discovery follows new libraries**: subscribers on `library.added`/`library.updated`
  queue a full scan, so a library added in the wizard is scanned right away.

## Consequences
- New runtime dependencies: FastAPI, Starlette, Uvicorn, Jinja2, python-multipart (and
  their dependencies); all on the license allow-list. Tests use `httpx2` (dev only).
- The security tests enumerate the app's routes, so a new core route or module page is
  covered automatically; adding a public route means editing the explicit list there.
- `SameSite=Strict` means following a link to steward-ai from another site shows the
  login page even when logged in (the cookie isn't sent on that first request).
- The SDK names FastAPI's `APIRouter` in `Registry.ui_page`, so modules with pages
  depend on FastAPI (already installed with steward-ai).
- Not yet: `steward setup --from setup.yaml`, `SetupStep` for modules, the model-endpoint
  step (needs the LLM layer), the folder settings pages (Settings will gain them).
