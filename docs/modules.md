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
`steward modules` lists it. Separately installed modules are **off** until enabled
in `modules.yaml`:

```yaml
config_version: 1
modules:
  hello:
    enabled: true
    settings:
      greeting: hi
```

## 3. Rules

- Import only `steward.sdk` and your own package. Nothing else from `steward`.
  (Built-in modules are checked by import-linter; out-of-tree modules should add the
  same contract to their own CI.)
- Never refer to a folder by name or position; read behavior from folder types.
- Write files only through an `ActionType` run by the executor (coming later in Phase 1).
- Call models only through the SDK's gateway, with provenance (coming with the LLM layer).
- Store data in tables prefixed `<module>_` (enforced when migrating), and register an
  exporter for anything that can't be rebuilt (exporters come with backups).
- Never put file contents in logs, events or notifications.

## Extension points available now

Every name a module registers is the module's name or starts with `<module>.`
(lowercase, dotted snake_case), so modules can't collide with each other.

| `Registry` method | Registers |
|---|---|
| `health_check(name, check)` | an `async def` returning `HealthResult("ok" \| "warn" \| "fail", detail)` |
| `job(name, handler, lane=...)` | a background job type; `handler(ctx: JobContext)` runs in a worker thread |
| `subscribe(event_type, handler, name=...)` | a reaction to events such as `file.discovered`; `handler(event: Event)` (core file events: `file.discovered`, `file.changed`, `file.moved`, `file.reappeared`, `file.missing`; payloads hold `file_id`, `library_id`, `relpath` and, once hashed, `content_hash`, `size`, `mime`) |
| `schedule(name, Every(...) \| Daily(...), job=..., payload=...)` | a recurring job; owners can re-time or turn it off in `pipeline.yaml` |
| `stage(name, run, version=, produces=, requires=, lane=, applies_to=, default=, index=)` | a pipeline stage; see "Stages" below |
| `migrations(path, database="state" \| "index")` | a directory of Alembic revisions creating `<module>_*` tables |
| `ui_page(name, router, path=, title=, nav=True, templates=, static=, badge=)` | a web UI page: a FastAPI `APIRouter` mounted at `path` (`/ui/<slug>`), optionally with Jinja2 templates and static files; `badge(state) -> int` puts a count next to its navigation entry; see "UI pages" below |
| `share_links(handler)` | answer the public share links `/s/<token>…` (one module at most); see "Share links" below |
| `link_builder(name, fn)` | `fn("library:path", "file" \| "dir") -> url \| None`: how to link to a file or folder in your pages (core pages such as search use the first one) |
| `action(ActionType)` | a kind of file change, run only by the executor; see "Actions" below |
| `exporter(fn)` | `fn(conn) -> {"<module>_table": [row dicts]}`: the module's irreplaceable data for `steward export` and backups |
| `config_as(Model)` | (not a registration) the module's validated settings, typed |

More are added as features need them.

```python
from datetime import timedelta
from pathlib import Path

from steward.sdk import Event, Every, JobContext, Registry


class NotesModule:
    ...  # name, version, kind, requires, config_model as above

    def register(self, r: Registry) -> None:
        r.job("notes.reindex", self.reindex, lane="cpu")
        r.subscribe("file.discovered", self.on_file, name="notes.on_file")
        r.schedule("notes.nightly", Every(timedelta(days=1)), job="notes.reindex")
        r.migrations(Path(__file__).parent / "migrations", database="state")

    def reindex(self, ctx: JobContext) -> None:
        # Must be idempotent. Raise to retry later; raise PermanentJobError to give up.
        # During long work, call ctx.renew_lease() (the lease is 5 minutes by default).
        ...

    def on_file(self, event: Event) -> None:
        # event.payload holds IDs and paths, never file content.
        ...
```

**Jobs** must be idempotent: after a crash, a job can run again. Lanes are `io`,
`cpu`, `llm-local`, `llm-remote` and `events` (worker process) and `executor` (API
process, the only writer). **Events** are delivered at least once, in no guaranteed
order. **Migrations**: each module has its own Alembic history and version table
([ADR 0007](adr/0007-per-owner-migrations.md)); write revision files by hand in the
module's `migrations/` directory with `revision`/`down_revision` like any Alembic
script.

## Module state

A module with `migrations(..., database="state")` reads and writes its own `<module>_*`
tables through a `StateAccess` handle (`steward.sdk.state`): `with state.read() as
conn:` / `with state.write() as conn:` give SQLAlchemy connections. Handles come with
each call: `JobContext.state`, `Web.state` (pages), `ShareRequest.state` (share links),
`ReadOnlyFS.read_state()` (planning an action); an action's own row changes go through
`GuardedFS.write_state(op)` so they commit with its journal entry. Store timestamps with
`steward.sdk.UTCDateTime` (also in migration files) and get "now" from `utcnow()`.
Never touch the core's tables.

## Stages

A stage computes something from a file's content and stores it in the module's own
`index.db` tables ([ADR 0011](adr/0011-stage-framework.md)). `examples/hello-module`
has a complete one.

```python
from steward.sdk import BlobInfo, FileInfo, FolderInfo, Registry, StageContext, StageResult


def is_pdf(blob: BlobInfo, file: FileInfo, folder: FolderInfo) -> bool:
    return blob.mime == "application/pdf"  # cheap and pure: no I/O


def extract(ctx: StageContext) -> StageResult | None:
    with ctx.open() as f:  # read-only; raises FileChanged if the file changed
        text = ...
    with ctx.index_write() as conn:  # upsert into docs_* tables, keyed by content hash
        ...
    return None  # done; or StageResult("skipped", reason="no text layer")


def register(r: Registry) -> None:
    r.stage(
        "docs.extract",
        extract,
        version=1,  # raise it whenever the output would change
        requires=("mime",),  # artifact kinds read; core.hash produces hash and mime
        produces=("text",),  # artifact kinds written; one producer per kind
        lane="cpu",
        applies_to=is_pdf,
    )
```

- Results are per **content**, not per path: identical copies run once, moves cost
  nothing. A stage can still run twice for the same content, so writes must be upserts.
- **Folder types decide where stages run.** `default=False` stages run only where a
  type adds them (`stages: {add: [extract]}`; full or short name); a type can also
  remove stages. Folders with `index: metadata` get only stages registered with
  `index="metadata"`; `index: none` folders get none.
- Raising `version` re-runs the stage everywhere, then everything that depends on it.
  Changes to `applies_to` alone aren't noticed: bump `version` if they matter.
- Raise to fail (retried with backoff; `PermanentJobError` gives up). A failed result
  isn't planned again until `steward jobs retry` or a version bump.
- Startup stops if two stages produce the same kind, a required kind has no producer
  (declare the module that provides it in `requires`), or stages form a cycle.

## Actions

Modules never write files directly. A change is an `ActionType` (from `steward.sdk`)
that the executor runs, checks, journals and can undo
([ADR 0014](adr/0014-executor-journal-undo.md)):

- `name` (`<module>.<verb>`), `params` (a pydantic model; paths are `LibraryPath`,
  written `library:relative/path`), `ai_mode` and `sensitive`.
- `plan(params, view)`: read-only (`view` is a `ReadOnlyFS`); returns a `Plan` listing
  `preconditions` (`PathState`s that must still hold), `reads`, `writes` (every path it
  may create or change) and any data `apply` needs. Raise `ActionRefused` for a clear
  "no".
- `render(plan)`: a `Rendered` summary for the preview and the journal.
- `apply(plan, fs)`: does the work through `GuardedFS` only (`mkdir`, `move`, `create`,
  `copy`, `replace`, `rmdir`, ...); returns `Applied` with the post-state.
- `inverse(plan, applied, view)`: params of an action that undoes this one, or `None`.
- `examples`: params over `EXAMPLE_TREE`; the executor-safety tests run them through
  undo round trips, drift, hostile paths, symlinks and collisions automatically.

Sensitive actions, and any action touching a locked folder, need the owner's
confirmation (a recent password check in the UI). A `Plan` can also set
`sensitive=True` when only some parameters make it sensitive (the core's `folder.lock`
does this for permanent locks); a plan can add sensitivity, never remove it.
`view.settings(path)` returns a `PathPolicy` (`type`, `writable`, `locked`,
`sensitivity`, `lock_from`: the folder whose lock applies, `permanent`).

## UI pages

```python
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

page = APIRouter()


@page.get("")
def overview() -> HTMLResponse:
    return HTMLResponse("<!doctype html><title>Notes</title><h1>Notes</h1>")


r.ui_page("notes.page", page, path="/ui/notes", title="Notes")
```

- The core mounts the router at `path`; its routes are relative to it (`""` is the
  page itself). The page gets a navigation entry unless `nav=False`.
- **Every route of a module page needs a logged-in browser session**, and every
  POST/PUT/PATCH/DELETE must carry the session's CSRF token (form field `csrf_token`
  or header `X-CSRF-Token`). The core enforces this for all routes; a module can't make
  a route public or skip the CSRF check. API tokens can't open UI pages.
- Only regular HTTP routes are allowed (no `add_route`, mounts or websockets); a page
  on a core path (`/ui/settings`, `/ui/jobs`, `/ui/health`, `/ui/proposals`,
  `/ui/journal`) or on another module's path stops startup.
- Pages are served with a strict Content-Security-Policy: no inline `<script>` or
  `style=""`. Escape anything you put into HTML (`html.escape`).
- Disabling the module removes the page and its navigation entry.

### Web services (`steward.sdk.web`)
Ask for the per-request `Web` handle with the `WebDep` dependency
([ADR 0017](adr/0017-module-web-services.md)):

```python
from pathlib import Path

from fastapi import APIRouter, Form
from fastapi.responses import Response

from steward.sdk import ActionRefused, WebDep

page = APIRouter()


@page.get("")
def folder(web: WebDep, path: str) -> Response:
    listing = web.files.listing(path)  # NotFound (404) for locked/hidden paths
    return web.render("folder.html", {"listing": listing})


@page.post("/mkdir")
def mkdir(web: WebDep, path: str = Form()) -> Response:
    try:
        web.actions.run("file.mkdir", {"path": path})  # journaled, as the owner
    except ActionRefused as exc:
        return web.render("folder.html", {"error": str(exc)}, status=409)
    return web.redirect("/ui/notes")


here = Path(__file__).parent
r.ui_page(
    "notes.page",
    page,
    path="/ui/notes",
    title="Notes",
    templates=here / "templates",
    static=here / "static",
)
```

- `web.render(name, context)` renders `templates/<name>`; templates may
  `{% extends "base.html" %}` and fill `title`, `head` and `content`; `notice` and
  `error` in the context show as messages; `form_token` is the CSRF token for forms and
  `static_url` is `/static/m/<module>`. Static files are public: never put anything
  private there.
- `web.files`: `libraries()`, `listing(path)`, `entry(path)`, `by_kind(kind)`,
  `search(query, folder=, kind=)` (both return a `FilePage`), `read_text(path,
  max_bytes=)`, `file_response(path, inline=)` (byte ranges, sandboxed),
  `zip_response(paths, name=)` (streamed). Every call takes `scope="normal"` (default:
  locked, hidden and trash folders don't exist) or `scope="locked"` (only inside locked
  folders, never their trash; asks for the password first). For the Locked Folder:
  `locked_folders()` (`LockedFolder`s to start from) and `unlock_preview(path)` (an
  `UnlockPreview`: what unlocking would make visible, or why it can't be unlocked).
  `privacy(paths)` gives each path's `PathPrivacy` (hidden in normal views? shareable,
  and why not?) without reading anything.
- `web.actions`: `preview`, `run`, `run_all` (one password check for a whole selection),
  `undo`. Sensitive actions send the browser to the password page by themselves.
- `web.viewer`, `web.recently_authenticated()`, `web.require_reauth()`, `web.redirect(url)`
  (same-site paths only), `web.state` (your tables), `web.page_url("<module>.page")`
  (another module's page, or None if it isn't enabled), `web.link_to(path, kind)` (the
  first link builder's link), `web.absolute_url("/path")` (a full URL for people
  outside, from `api.public_url` when set).
- Raw `Response`s you build yourself get the page CSP; never serve file content that
  way, use `file_response`.

### Share links (`steward.sdk.shares`)

The core owns `/s/<token>` and `/s/<token>/<path>`; a module registered with
`share_links(handler)` answers them (the built-in `shares` module does). The handler
gets a `ShareRequest` (token, `subpath`, `method`, `query`, `form`, `client_ip`,
`user_agent`, `state`, `form_token`, `base_url`, `cookie()`, `set_cookie()`,
`render()` with templates extending `public.html`, `redirect()`, `not_found()`) and
calls `open("library:path", "file" | "dir", only_files=...)` for the item: the core
refuses (`NotFound`) anything that may not be shared right now and confines the
returned `SharedItem` (`entry`, `listing`, `read_text`, `file_response`,
`zip_response`, all taking paths relative to the item) to what inside it may be shared.
Use `ReadOnlyFS.privacy(path)` / `Files.privacy(paths)` (`PathPrivacy`) to check the
same rule earlier, and keep secrets out of the journal with `Plan.transient`.

## Loader errors

Startup stops (fails closed) when an enabled module isn't installed, two packages
provide the same module name, a module uses a reserved name (`core`, `steward`), a
module doesn't match the contract, a required module isn't enabled, modules require
each other in a cycle, a config section doesn't validate, a name is registered twice,
or a schedule names a job type that isn't registered.
