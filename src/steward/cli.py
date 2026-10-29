# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The `steward` command line."""

from __future__ import annotations

import json
import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Annotated, NoReturn, cast

import typer
from sqlalchemy import func, select

import steward
from steward.core import auth as steward_auth
from steward.core import backup, content, queue, stages
from steward.core import logging as steward_logging
from steward.core.builtin import tag_stage
from steward.core.config import (
    CONFIG_DIR_ENV,
    DEFAULT_CONFIG_DIR,
    Config,
    ConfigError,
    load_config,
    load_folders_file,
)
from steward.core.db import migrate, utcnow
from steward.core.db.schema import blobs as blobs_table
from steward.core.db.schema import files as files_table
from steward.core.db.schema import stage_runs
from steward.core.discovery import RECONCILE_JOB, ScanError, Scanner, Watcher, enqueue_scan
from steward.core.executor import ActionFailed
from steward.core.folders import (
    ROLES,
    FolderError,
    FolderRef,
    FolderTypeError,
    PathError,
    load_policy,
)
from steward.core.folders import store as folder_store
from steward.core.folders.seed import import_seed
from steward.core.loader import ModuleLoadError, discover, is_builtin
from steward.core.privacy import PrivacyRules, set_override
from steward.core.runtime import Runtime, build_runtime
from steward.core.search import Mode, SearchError, SearchFilters, search_all, tag_counts
from steward.core.stages import rerun
from steward.core.tags import TagError
from steward.core.tags import bootstrap as tag_bootstrap
from steward.core.tags import effective as tag_effective
from steward.core.tags import io as tag_io
from steward.core.tags import store as tag_store
from steward.sdk import ActionRefused, ModelUnavailable

app = typer.Typer(
    name="steward",
    help="steward-ai: a self-hosted AI steward for the files on your server.",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(help="Check configuration.", no_args_is_help=True)
db_app = typer.Typer(help="Database migrations.", no_args_is_help=True)
jobs_app = typer.Typer(help="Inspect and retry background jobs.", no_args_is_help=True)
folders_app = typer.Typer(
    help="Libraries, folder mappings, locks and roles. Folders are written library:path.",
    no_args_is_help=True,
)
types_app = typer.Typer(help="Folder types (built-in and custom).", no_args_is_help=True)
files_app = typer.Typer(help="Files found by discovery.", no_args_is_help=True)
stages_app = typer.Typer(help="Pipeline stages and their results.", no_args_is_help=True)
user_app = typer.Typer(
    help="The owner account: password recovery, lockouts, sessions, login settings.",
    no_args_is_help=True,
)
tokens_app = typer.Typer(help="API tokens (scopes: read, propose).", no_args_is_help=True)
journal_app = typer.Typer(help="Applied actions: list them and undo them.", no_args_is_help=True)
models_app = typer.Typer(help="Model roles and the models mapped to them.", no_args_is_help=True)
tags_app = typer.Typer(help="Tags: the vocabulary and files' tags.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(jobs_app, name="jobs")
app.add_typer(folders_app, name="folders")
app.add_typer(types_app, name="types")
app.add_typer(files_app, name="files")
app.add_typer(stages_app, name="stages")
app.add_typer(user_app, name="user")
app.add_typer(tokens_app, name="tokens")
app.add_typer(journal_app, name="journal")
app.add_typer(models_app, name="models")
app.add_typer(tags_app, name="tags")

CLI_ACTOR = "cli"

_state: dict[str, Path] = {}


@app.callback()
def main(
    config_dir: Annotated[
        Path,
        typer.Option(
            "--config-dir",
            envvar=CONFIG_DIR_ENV,
            help="Directory with steward.yaml, modules.yaml, pipeline.yaml.",
        ),
    ] = DEFAULT_CONFIG_DIR,
) -> None:
    _state["config_dir"] = config_dir


def _config() -> Config:
    try:
        return load_config(_state.get("config_dir", DEFAULT_CONFIG_DIR))
    except ConfigError as exc:
        _fail(str(exc))


def _runtime() -> Runtime:
    config = _config()
    try:
        runtime = build_runtime(config)
    except (ModuleLoadError, ConfigError) as exc:
        _fail(str(exc))
    for warning in runtime.warnings:
        typer.echo(f"warning: {warning}", err=True)
    return runtime


def _fail(message: str) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(1)


@app.command()
def version() -> None:
    """Print the steward-ai version."""
    typer.echo(steward.__version__)


@app.command()
def modules() -> None:
    """List installed modules, whether each is enabled, and what enabled ones register."""
    runtime = _runtime()
    loaded = {m.module.name: m for m in runtime.modules.modules}
    installed = discover()
    if not installed:
        typer.echo("No modules installed.")
        return
    for name, ep in sorted(installed.items()):
        m = loaded.get(name)
        origin = "built-in" if is_builtin(ep) else "installed"
        if m is None:
            typer.echo(f"{name} (disabled; {origin})")
            continue
        mod = m.module
        requires = ", ".join(mod.requires) or "-"
        typer.echo(f"{name} {mod.version} (enabled; {mod.kind}; {origin}; requires: {requires})")
        typer.echo(f"  provided by: {m.provider or 'unknown'}")
        for reg in runtime.registrations.by_module(name):
            typer.echo(f"  {reg.point}: {reg.name}")


@config_app.command("check")
def config_check() -> None:
    """Validate the config files and the enabled modules' settings."""
    runtime = _runtime()
    cfg = runtime.config
    typer.echo(f"config dir: {cfg.config_dir}")
    typer.echo(f"data dir:   {cfg.data_dir}")
    typer.echo(f"timezone:   {cfg.steward.timezone}")
    typer.echo(f"modules:    {', '.join(runtime.modules.names()) or 'none enabled'}")
    for name, sched in sorted(runtime.schedules.items()):
        typer.echo(f"schedule:   {name}: {sched.when}")
    typer.echo("OK")


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Create the databases if needed and apply all pending migrations."""
    runtime = _runtime()
    try:
        taken = backup.before_migrate(runtime)
        if taken is not None:
            typer.echo(f"backed up to {taken.path} before migrating")
        changed = runtime.upgrade_databases()
    except (migrate.MigrationError, backup.BackupError) as exc:
        _fail(str(exc))
    for database, owners in changed.items():
        typer.echo(f"{database}: {'upgraded ' + ', '.join(owners) if owners else 'up to date'}")


@db_app.command("status")
def db_status() -> None:
    """Show each migration owner's current and latest revision."""
    runtime = _runtime()
    for name in ("state", "index"):
        db = runtime.database(name)
        if not db.exists():
            typer.echo(f"{name}: no database yet ({db.path})")
            continue
        for s in migrate.status(db, runtime.migration_owners(name)):
            mark = "ok" if s.up_to_date else "needs upgrade"
            typer.echo(f"{name}: {s.owner} at {s.current or 'nothing'} (head {s.head}) {mark}")


@jobs_app.command("list")
def jobs_list(
    status: Annotated[str | None, typer.Option(help="queued, running, done or dead")] = None,
    limit: int = 30,
) -> None:
    """Show recent jobs."""
    runtime = _runtime()
    with runtime.dbs.state.read() as conn:
        rows = queue.list_jobs(conn, status=status, limit=limit)
    for j in rows:
        error = f"  error: {j.last_error}" if j.last_error and j.status != "done" else ""
        typer.echo(
            f"{j.job_id:>6} {j.status:<8} {j.lane:<10} {j.type} "
            f"(attempt {j.attempts}/{j.max_attempts}){error}"
        )


@jobs_app.command("retry")
def jobs_retry(job_id: int) -> None:
    """Put a dead job back in the queue."""
    runtime = _runtime()
    with runtime.dbs.state.write() as conn:
        ok = queue.retry(conn, job_id)
    if not ok:
        _fail(f"job {job_id} doesn't exist or isn't dead")
    typer.echo(f"job {job_id} queued again")


@app.command()
def worker() -> None:
    """Run the background worker until stopped (SIGTERM or Ctrl-C)."""
    from steward.core.worker import Worker

    runtime = _runtime()
    steward_logging.configure(runtime.config.steward.log_level)
    try:
        runtime.ensure_databases_current()
    except migrate.MigrationError as exc:
        _fail(str(exc))

    stop = threading.Event()

    def on_signal(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    discovery = runtime.config.pipeline.discovery
    if discovery.scan_on_start:
        with runtime.dbs.state.write() as conn:
            # Same key as the schedule's, so a first start doesn't queue two.
            queue.enqueue(conn, RECONCILE_JOB, lane="io", dedupe_key=f"schedule:{RECONCILE_JOB}")
    if stages.full_pass_needed(runtime.dbs, runtime.stages):
        with runtime.dbs.state.write() as conn:
            stages.enqueue_full_pass(conn, max_attempts=runtime.config.pipeline.jobs.max_attempts)
    watcher_thread = None
    if discovery.watch:
        watcher = Watcher(
            runtime.dbs, discovery, max_attempts=runtime.config.pipeline.jobs.max_attempts
        )
        watcher_thread = threading.Thread(target=watcher.run, args=(stop,), name="watcher")
        watcher_thread.start()
    Worker(runtime).run(stop)
    if watcher_thread is not None:
        watcher_thread.join(timeout=15)
    runtime.close()


@app.command()
def api() -> None:
    """Run the web UI and API (migrates the databases first). Prints a setup token while
    no owner account exists."""
    from steward.api import UiPageError, serve

    runtime = _runtime()
    steward_logging.configure(runtime.config.steward.log_level)
    try:
        serve(runtime)
    except (migrate.MigrationError, backup.BackupError, UiPageError) as exc:
        _fail(str(exc))
    finally:
        runtime.close()


# --- folders and types ------------------------------------------------------------


def _ready_runtime() -> Runtime:
    runtime = _runtime()
    try:
        runtime.ensure_databases_current()
    except migrate.MigrationError as exc:
        _fail(str(exc))
    return runtime


def _ref(text: str) -> FolderRef:
    try:
        return FolderRef.parse(text)
    except PathError as exc:
        _fail(str(exc))


@contextmanager
def _folder_errors() -> Iterator[None]:
    try:
        yield
    except (FolderError, FolderTypeError, PathError, ConfigError) as exc:
        _fail(str(exc))


@folders_app.command("import")
def folders_import(
    path: Annotated[
        Path | None,
        typer.Argument(
            help="A file in folders.yaml format. Default: the config dir's folders.yaml."
        ),
    ] = None,
) -> None:
    """Import libraries.yaml and folders.yaml (or PATH) into the database. Only adds
    and updates; never removes anything or lifts a permanent lock."""
    runtime = _ready_runtime()
    with _folder_errors():
        folders_cfg = load_folders_file(path) if path else runtime.config.folders
        with runtime.dbs.state.write() as conn:
            report = import_seed(
                conn, libraries=runtime.config.libraries, folders=folders_cfg, actor=CLI_ACTOR
            )
    for lib in runtime.config.libraries.libraries.values():
        if not Path(lib.path).is_dir():
            typer.echo(f"warning: library root {lib.path} isn't a directory here", err=True)
    typer.echo(
        f"libraries added: {', '.join(report.libraries_added) or 'none'}; "
        f"types created/changed: {', '.join(report.types_defined) or 'none'}; "
        f"mappings applied: {report.mappings}; roles set: {', '.join(report.roles) or 'none'}"
    )
    if report.default_roles:
        typer.echo(f"default folders assigned for: {', '.join(report.default_roles)}")


@folders_app.command("list")
def folders_list() -> None:
    """Show libraries, mapped folders and roles."""
    runtime = _ready_runtime()
    with runtime.dbs.state.read() as conn:
        policy = load_policy(conn)
    if not policy.libraries:
        typer.echo("No libraries yet. Add them to libraries.yaml and run `steward folders import`.")
        return
    for lib in sorted(policy.libraries.values(), key=lambda lib: lib.position):
        typer.echo(f"{lib.library_id}: {lib.root} ({lib.name})")
        for ref, m in sorted(policy.mappings.items()):
            if ref.library_id != lib.library_id:
                continue
            extras = []
            if m.locked is not None:
                extras.append("locked" if m.locked else "unlocked")
            if m.permanent:
                extras.append("permanent")
            label = m.type_name or "(inherits type)"
            suffix = f" [{', '.join(extras)}]" if extras else ""
            typer.echo(f"  {ref.relpath or '/'}: {label}{suffix}")
    for role, ref in sorted(policy.roles.items()):
        typer.echo(f"role {role}: {ref}")


@folders_app.command("show")
def folders_show(folder: str) -> None:
    """Show the effective settings for a folder, and why."""
    runtime = _ready_runtime()
    ref = _ref(folder)
    with runtime.dbs.state.read() as conn, _folder_errors():
        settings = load_policy(conn).settings_for(ref)
        roles = load_policy(conn).role_at(ref)
    source = (
        f"mapped at {ref.library_id}:{settings.type_from}"
        if settings.type_from is not None
        else "library default"
    )
    typer.echo(f"{ref}")
    typer.echo(
        f"  type:          {settings.type.name} ({source}; chain {' > '.join(settings.type.chain)})"
    )
    typer.echo(f"  description:   {settings.type.description}")
    typer.echo(f"  index / hash:  {settings.index} / {settings.hash}")
    typer.echo(f"  sensitivity:   {settings.sensitivity}")
    lock = "no"
    if settings.locked:
        permanent = ", permanent" if settings.permanent else ""
        lock = f"yes (by {ref.library_id}:{settings.locked_by}){permanent}"
    typer.echo(f"  locked:        {lock}")
    typer.echo(f"  writable:      {settings.writable}")
    typer.echo(f"  shareable:     {settings.shareable}")
    typer.echo(f"  triage target: {settings.triage_target}")
    typer.echo(f"  visible:       {settings.visible}")
    if settings.stages_add or settings.stages_remove:
        typer.echo(
            f"  stages:        +{','.join(sorted(settings.stages_add)) or '-'} "
            f"-{','.join(sorted(settings.stages_remove)) or '-'}"
        )
    for name, value in (
        ("default tags", settings.default_tags),
        ("fields", settings.fields),
        ("ignore", settings.ignore),
    ):
        if value:
            typer.echo(f"  {name + ':':<14} {', '.join(value)}")
    if roles:
        typer.echo(f"  roles:         {', '.join(roles)}")
    for note in settings.notes:
        typer.echo(f"  note: {note}")


@folders_app.command("map")
def folders_map(folder: str, type_name: str) -> None:
    """Map FOLDER to a folder type."""
    runtime = _ready_runtime()
    with _folder_errors(), runtime.dbs.state.write() as conn:
        folder_store.set_type(conn, _ref(folder), type_name, actor=CLI_ACTOR)
    typer.echo(f"{folder} -> {type_name}")


@folders_app.command("unmap")
def folders_unmap(folder: str) -> None:
    """Remove FOLDER's own type; it inherits its parent's again."""
    runtime = _ready_runtime()
    with _folder_errors(), runtime.dbs.state.write() as conn:
        folder_store.set_type(conn, _ref(folder), None, actor=CLI_ACTOR)
    typer.echo(f"{folder} now inherits its type")


@folders_app.command("lock")
def folders_lock(
    folder: str,
    permanent: Annotated[
        bool, typer.Option(help="Only removable with `unlock --permanent` here.")
    ] = False,
) -> None:
    """Lock FOLDER and everything inside it."""
    runtime = _ready_runtime()
    with _folder_errors(), runtime.dbs.state.write() as conn:
        folder_store.lock(conn, _ref(folder), permanent=permanent, actor=CLI_ACTOR)
    typer.echo(f"{folder} locked{' permanently' if permanent else ''}")


@folders_app.command("unlock")
def folders_unlock(
    folder: str,
    permanent: Annotated[bool, typer.Option(help="Also remove a permanent lock.")] = False,
) -> None:
    """Unlock FOLDER. Its contents become visible outside the Locked Folder."""
    runtime = _ready_runtime()
    with _folder_errors(), runtime.dbs.state.write() as conn:
        folder_store.unlock(conn, _ref(folder), allow_permanent=permanent, actor=CLI_ACTOR)
    typer.echo(f"{folder} unlocked")


@folders_app.command("role")
def folders_role(
    role: Annotated[str, typer.Argument(help="trash, edit_backups, inbox or digest_output")],
    folder: Annotated[str | None, typer.Argument(help="Omit to clear the role.")] = None,
) -> None:
    """Point a role at a folder, or clear it."""
    if role not in ROLES:
        _fail(f"unknown role {role!r}; roles are {', '.join(ROLES)}")
    runtime = _ready_runtime()
    with _folder_errors(), runtime.dbs.state.write() as conn:
        if folder is None:
            folder_store.clear_role(conn, role, actor=CLI_ACTOR)
        else:
            folder_store.set_role(conn, role, _ref(folder), actor=CLI_ACTOR)
    typer.echo(f"role {role}: {folder or 'cleared'}")


@folders_app.command("move")
def folders_move(folder: str, new_path: str) -> None:
    """Record that FOLDER was renamed or moved to NEW_PATH (same library) outside
    steward-ai, so its mapping, lock and roles follow it. Doesn't touch the disk."""
    runtime = _ready_runtime()
    with _folder_errors(), runtime.dbs.state.write() as conn:
        moved = folder_store.move_folder(conn, _ref(folder), new_path, actor=CLI_ACTOR)
    typer.echo(f"moved {moved} folder identit{'y' if moved == 1 else 'ies'}")


@types_app.command("list")
def types_list() -> None:
    """List folder types."""
    runtime = _ready_runtime()
    with runtime.dbs.state.read() as conn:
        policy = load_policy(conn)
    for name, t in sorted(policy.types.items(), key=lambda kv: (not kv[1].builtin, kv[0])):
        origin = "built-in" if t.builtin else f"custom, extends {t.chain[1]}"
        typer.echo(f"{name:<14} {origin:<28} {t.description}")


@types_app.command("show")
def types_show(name: str) -> None:
    """Show a type's resolved settings."""
    runtime = _ready_runtime()
    with runtime.dbs.state.read() as conn:
        policy = load_policy(conn)
    t = policy.types.get(name)
    if t is None:
        _fail(f"unknown folder type {name!r}")
    typer.echo(f"{t.name} ({'built-in' if t.builtin else 'custom'}; chain {' > '.join(t.chain)})")
    for key in (
        "description",
        "index",
        "hash",
        "sensitivity",
        "locked",
        "writable",
        "shareable",
        "triage_target",
        "visible",
        "default_tags",
        "fields",
        "ignore",
    ):
        typer.echo(f"  {key}: {getattr(t, key)}")
    typer.echo(f"  stages: +{sorted(t.stages_add)} -{sorted(t.stages_remove)}")


@types_app.command("rename")
def types_rename(old: str, new: str) -> None:
    """Rename a custom type (mappings follow)."""
    runtime = _ready_runtime()
    with _folder_errors(), runtime.dbs.state.write() as conn:
        folder_store.rename_type(conn, old, new, actor=CLI_ACTOR)
    typer.echo(f"{old} -> {new}")


@types_app.command("delete")
def types_delete(name: str) -> None:
    """Delete a custom type that no folder uses."""
    runtime = _ready_runtime()
    with _folder_errors(), runtime.dbs.state.write() as conn:
        folder_store.delete_type(conn, name, actor=CLI_ACTOR)
    typer.echo(f"deleted {name}")


# --- discovery ------------------------------------------------------------------------


@app.command()
def scan(
    targets: Annotated[
        list[str] | None,
        typer.Argument(help="library or library:path (default: every library)"),
    ] = None,
    allow_empty: Annotated[
        bool,
        typer.Option(help="Accept an empty library root (normally a sign of an unmounted disk)."),
    ] = False,
) -> None:
    """Scan libraries now, in the foreground, and print what changed. The worker does
    the same on its own (on start, every 6 hours, and when files change)."""
    runtime = _ready_runtime()
    steward_logging.configure(runtime.config.steward.log_level)
    with runtime.dbs.state.read() as conn:
        known = list(load_policy(conn).libraries)
    by_library: dict[str, list[str]] = {}
    for target in targets or known:
        library_id, _, relpath = target.partition(":")
        if library_id not in known:
            _fail(f"unknown library {library_id!r}")
        by_library.setdefault(library_id, []).append(relpath.strip("/"))
    scanner = Scanner(
        runtime.dbs,
        runtime.config.pipeline.discovery,
        actor=CLI_ACTOR,
        allow_empty_root=allow_empty,
        privacy=PrivacyRules.from_config(runtime.config.privacy),
    )
    for library_id, paths in by_library.items():
        try:
            report = scanner.scan(library_id, paths)
        except ScanError as exc:
            _fail(str(exc))
        counts = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in report.as_dict().items() if v)
        typer.echo(f"{library_id}: {counts or 'nothing found'}")


@files_app.command("list")
def files_list(
    folder: Annotated[str | None, typer.Argument(help="library:path (default: everything)")] = None,
    missing: Annotated[bool, typer.Option(help="Only files that have gone missing.")] = False,
    limit: int = 50,
) -> None:
    """List known files: path, size, content hash, and whether they're present."""
    runtime = _ready_runtime()
    query = select(files_table).order_by(files_table.c.library_id, files_table.c.relpath)
    if folder:
        ref = _ref(folder)
        query = query.where(files_table.c.library_id == ref.library_id)
        if ref.relpath:
            prefix = ref.relpath + "/"
            query = query.where(
                (files_table.c.relpath == ref.relpath)
                | (func.substr(files_table.c.relpath, 1, len(prefix)) == prefix)
            )
    if missing:
        query = query.where(files_table.c.present.is_(False))
    with runtime.dbs.state.read() as conn:
        rows = conn.execute(query.limit(limit)).all()
        total = conn.execute(select(func.count()).select_from(files_table)).scalar_one()
    for r in rows:
        state = "" if r.present else "  [missing]"
        digest = (
            r.content_hash[:20]
            if r.content_hash
            else f"(not hashed: {r.error or 'pending or excluded'})"
        )
        typer.echo(f"{r.library_id}:{r.relpath}  {r.size} B  {digest}{state}")
    typer.echo(f"({len(rows)} shown, {total} known)")


@files_app.command("info")
def files_info(
    path: Annotated[str, typer.Argument(help="library:path of a file")],
) -> None:
    """Show what steward-ai knows about a file: its content hash and type, each stage's
    result, and the properties stages stored (date taken, camera…). On the host only."""
    runtime = _ready_runtime()
    ref = _ref(path)
    with runtime.dbs.state.read() as conn:
        row = conn.execute(
            select(files_table).where(
                files_table.c.library_id == ref.library_id,
                files_table.c.relpath == ref.relpath,
                files_table.c.present.is_(True),
            )
        ).one_or_none()
    if row is None:
        _fail(f"{path} isn't a known file (run `steward scan` first?)")
    typer.echo(f"{path}\n  file id {row.file_id}  {row.size} B")
    if row.content_hash is None:
        typer.echo("  not hashed (pending, unreadable or excluded)")
        return
    with runtime.dbs.index.read() as conn:
        blob = conn.execute(
            select(blobs_table.c.mime, blobs_table.c.kind).where(
                blobs_table.c.content_hash == row.content_hash
            )
        ).one_or_none()
        runs = conn.execute(
            select(stage_runs.c.stage, stage_runs.c.status, stage_runs.c.error)
            .where(stage_runs.c.content_hash == row.content_hash)
            .order_by(stage_runs.c.stage)
        ).all()
        props = content.properties_of(conn, row.content_hash)
    typer.echo(f"  content {row.content_hash} ({row.hash_kind})")
    if blob is not None:
        typer.echo(f"  type {blob.mime} ({blob.kind})")
    typer.echo("stages:")
    for run in runs:
        note = f" ({run.error})" if run.error else ""
        typer.echo(f"  {run.stage}: {run.status}{note}")
    for artifact, values in props.items():
        typer.echo(f"{artifact}:")
        for key, value in values.items():
            typer.echo(f"  {key}: {value}")
    with runtime.dbs.state.read() as state, runtime.dbs.index.read() as index:
        found = tag_effective.file_tags(
            state,
            index,
            load_policy(state),
            file_id=row.file_id,
            content_hash=row.content_hash,
            library_id=ref.library_id,
            relpath=ref.relpath,
        )
    if found:
        typer.echo("tags:")
        for t in found:
            typer.echo(f"  {t.name} ({t.source}{', ' + t.status if t.status != 'active' else ''})")


@files_app.command("text")
def files_text(
    path: Annotated[str, typer.Argument(help="library:path of a file")],
) -> None:
    """Print the text steward-ai extracted from a file (on the host only; this shows
    file content, including from locked folders)."""
    runtime = _ready_runtime()
    ref = _ref(path)
    with runtime.dbs.state.read() as conn:
        row = conn.execute(
            select(files_table.c.content_hash).where(
                files_table.c.library_id == ref.library_id, files_table.c.relpath == ref.relpath
            )
        ).one_or_none()
    if row is None:
        _fail(f"{path} isn't a known file (run `steward scan` first?)")
    if row.content_hash is None:
        _fail(f"{path} hasn't been hashed (pending, unreadable or excluded)")
    with runtime.dbs.index.read() as conn:
        found = content.text_of(conn, row.content_hash)
    if not found:
        _fail("no text extracted (yet); see `steward stages list`")
    for artifact, parts in found.items():
        typer.echo(f"== {artifact} ({len(parts)} parts)")
        for part in parts:
            where = "  ".join(
                x for x in (f"page {part.page}" if part.page else "", part.heading or "") if x
            )
            if where:
                typer.echo(f"-- {where}")
            typer.echo(part.body)


# --- stages ---------------------------------------------------------------------


@stages_app.command("list")
def stages_list() -> None:
    """Stages in dependency order, with how many contents each has done/skipped/failed."""
    runtime = _ready_runtime()
    with runtime.dbs.index.read() as conn:
        counts: dict[str, dict[str, int]] = {}
        for stage, status, n in conn.execute(
            select(stage_runs.c.stage, stage_runs.c.status, func.count()).group_by(
                stage_runs.c.stage, stage_runs.c.status
            )
        ):
            counts.setdefault(stage, {})[status] = n
    for name, stage in runtime.stages.stages.items():
        requires = ", ".join(stage.requires) or "-"
        if stage.optional:  # read when available
            requires += f" [{', '.join(stage.optional)}]"
        flags = "" if stage.default else "  (only where a folder type adds it)"
        if stage.roles:
            flags += f"  (model roles: {', '.join(stage.roles)})"
        done = counts.get(name, {})
        status = "  ".join(f"{k} {v}" for k, v in sorted(done.items())) or "no results"
        typer.echo(
            f"{name} v{stage.version}  [{stage.lane}]  {requires} -> {', '.join(stage.produces)}"
            f"{flags}\n    {status}"
        )
    for name, reason in sorted(runtime.stages.inactive.items()):
        typer.echo(f"{name}  (not running: {reason})")


@stages_app.command("plan")
def stages_plan() -> None:
    """Queue a full planning pass (the worker runs it): every file gets the stages it lacks."""
    runtime = _ready_runtime()
    with runtime.dbs.state.write() as conn:
        job_id = stages.enqueue_full_pass(
            conn, max_attempts=runtime.config.pipeline.jobs.max_attempts
        )
    typer.echo(f"queued {stages.FULL_JOB} (job {job_id})")


# --- models -------------------------------------------------------------------------


@models_app.command("list")
def models_list() -> None:
    """Roles the enabled modules use, the model each is mapped to in llm.yaml, and the
    stages that call them."""
    runtime = _runtime()
    graph = runtime.stages
    if not graph.roles:
        typer.echo("no enabled module uses a model role")
    for name, state in graph.roles.items():
        target = f"{state.endpoint}/{state.model}" if state.model else "not mapped"
        problem = f"  ({state.problem})" if state.problem else ""
        typer.echo(f"{name} [{state.kind}] -> {target}{problem}")
        typer.echo(f"    {state.description} (declared by {', '.join(state.modules)})")
        users = [s for s, st in graph.stages.items() if name in st.roles]
        users += [s for s in graph.inactive if name in runtime.registrations.stages()[s].roles]
        if users:
            typer.echo(f"    stages: {', '.join(sorted(users))}")
    unused = sorted(set(runtime.config.llm.roles) - set(graph.roles))
    if unused:
        typer.echo(f"mapped in llm.yaml but used by no enabled module: {', '.join(unused)}")


@models_app.command("probe")
def models_probe(
    role: Annotated[str | None, typer.Argument(help="Only this role.")] = None,
) -> None:
    """Send test requests to each mapped role's model (no file content) and report
    what works: embeddings and their size; replies, structured JSON, tool calls and
    images for chat roles, as configured."""
    from steward.core.llm import probe

    runtime = _ready_runtime()
    roles = runtime.config.llm.roles
    names = [role] if role else sorted(roles)
    if role and role not in roles:
        _fail(f"role {role!r} isn't mapped in llm.yaml")
    if not names:
        _fail("llm.yaml maps no roles")
    failed = False
    for name in names:
        state = runtime.stages.roles.get(name)
        kind = state.kind if state else ("embed" if roles[name].dims else "chat")
        typer.echo(f"{name} [{kind}] -> {roles[name].endpoint}/{roles[name].model}")
        for check in probe.probe_role(runtime.gateway, name, kind):
            failed |= not check.ok
            typer.echo(f"    {'ok  ' if check.ok else 'FAIL'}  {check.name}: {check.detail}")
    if failed:
        raise typer.Exit(1)


# --- user and tokens ----------------------------------------------------------------


@contextmanager
def _auth_errors() -> Iterator[None]:
    try:
        yield
    except (steward_auth.AuthError, steward_auth.WeakPasswordError) as exc:
        _fail(str(exc))


def _when(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "-"


def _owner(auth: steward_auth.Auth, username: str | None) -> steward_auth.User:
    with _auth_errors():
        if username is not None:
            return auth.get_user(username=username)
    found = auth.list_users()
    if not found:
        _fail("there is no account yet; create the owner in the setup wizard")
    return found[0]


@user_app.command("list")
def user_list() -> None:
    """Show accounts."""
    runtime = _ready_runtime()
    found = runtime.auth.list_users()
    if not found:
        typer.echo("No accounts yet (the setup wizard creates the owner).")
    for u in found:
        state = "  [disabled]" if u.disabled_at else ""
        typer.echo(
            f"{u.user_id}  {u.username}  {u.role}  created {_when(u.created_at)}  "
            f"password changed {_when(u.password_changed_at)}{state}"
        )


@user_app.command("reset-password")
def user_reset_password(
    username: Annotated[str | None, typer.Argument(help="Default: the owner.")] = None,
) -> None:
    """Set a new password from the host (recovery). Ends every session of the account
    and lifts its lockouts. API tokens keep working; revoke them with `steward tokens`."""
    runtime = _ready_runtime()
    auth = runtime.auth
    user = _owner(auth, username)
    password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
    with _auth_errors():
        ended = auth.reset_password(user.username, password, actor=CLI_ACTOR)
    typer.echo(f"password changed for {user.username}; {ended} session(s) ended")


@user_app.command("unlock")
def user_unlock(
    username: Annotated[str | None, typer.Option(help="Forget failures for this username.")] = None,
    ip: Annotated[str | None, typer.Option(help="Forget failures from this address.")] = None,
) -> None:
    """Lift a login lockout early. Default: the owner's username."""
    runtime = _ready_runtime()
    auth = runtime.auth
    if username is None and ip is None:
        username = _owner(auth, None).username
    with _auth_errors():
        forgotten = auth.unlock(username=username, ip=ip)
    typer.echo(f"forgot {forgotten} failed attempt(s)")


@user_app.command("sessions")
def user_sessions(
    all_: Annotated[bool, typer.Option("--all", help="Include ended sessions.")] = False,
) -> None:
    """List login sessions, newest first."""
    runtime = _ready_runtime()
    now = utcnow()
    found = runtime.auth.list_sessions(include_ended=all_, now=now)
    if not found:
        typer.echo("No sessions.")
    for s in found:
        if s.revoked_at:
            state = f"ended {_when(s.revoked_at)} ({s.revoked_reason})"
        elif s.active(now):
            state = f"active until {_when(s.expires_at)}"
        else:
            state = f"expired {_when(s.expires_at)}"
        typer.echo(
            f"{s.session_id}  user {s.user_id}  from {s.ip or '?'}  "
            f"since {_when(s.created_at)}  last seen {_when(s.last_seen)}  {state}"
        )
        if s.user_agent:
            typer.echo(f"    {s.user_agent}")


@user_app.command("logout")
def user_logout(
    session_id: Annotated[int | None, typer.Argument(help="Default: every session.")] = None,
) -> None:
    """End one session, or all of them."""
    runtime = _ready_runtime()
    auth = runtime.auth
    if session_id is not None:
        if not auth.revoke_session(session_id, actor=CLI_ACTOR):
            _fail(f"no active session {session_id}")
        typer.echo(f"session {session_id} ended")
        return
    ended = sum(auth.revoke_all_sessions(u.user_id, actor=CLI_ACTOR) for u in auth.list_users())
    typer.echo(f"{ended} session(s) ended")


@user_app.command("settings")
def user_settings(
    reauth: Annotated[
        bool | None,
        typer.Option(
            "--reauth/--no-reauth",
            help="Ask for the password again before sensitive steps (and locked folders).",
        ),
    ] = None,
    reauth_minutes: Annotated[
        int | None,
        typer.Option(help="How long a password check counts as recent; 0 = every time."),
    ] = None,
) -> None:
    """Show or change login settings (the same ones as in the web UI's Settings)."""
    runtime = _ready_runtime()
    auth = runtime.auth
    changes: dict[str, object] = {}
    if reauth is not None:
        changes["reauth_enabled"] = reauth
    if reauth_minutes is not None:
        changes["reauth_minutes"] = reauth_minutes
    with _auth_errors():
        current = (
            auth.set_settings_from_host(changes, actor=CLI_ACTOR) if changes else auth.settings()
        )
    if current.reauth_enabled:
        window = f"{current.reauth_minutes} minute(s)" if current.reauth_minutes else "every time"
        typer.echo(f"re-auth: on, asks again after {window}")
    else:
        typer.echo("re-auth: OFF")
        typer.echo(
            "warning: sensitive steps and locked folders now need only a normal login", err=True
        )
    login = auth.config.login
    typer.echo(
        f"sessions: end after {auth.config.session_idle_hours:g} h idle or "
        f"{auth.config.session_max_days:g} days (steward.yaml)"
    )
    typer.echo(
        f"lockout: {login.max_failures_per_ip} failures per address or "
        f"{login.max_failures_per_account} per username within {login.window_minutes} min "
        f"-> {login.lockout_minutes} min (steward.yaml)"
    )


@tokens_app.command("list")
def tokens_list(
    all_: Annotated[bool, typer.Option("--all", help="Include revoked and expired.")] = False,
) -> None:
    """List API tokens (secrets are never shown again)."""
    runtime = _ready_runtime()
    now = utcnow()
    found = [t for t in runtime.auth.list_tokens() if all_ or t.active(now)]
    if not found:
        typer.echo("No API tokens.")
    for t in found:
        if t.revoked_at:
            state = f"revoked {_when(t.revoked_at)}"
        elif not t.active(now):
            state = f"expired {_when(t.expires_at)}"
        else:
            state = f"expires {_when(t.expires_at)}" if t.expires_at else "no expiry"
        typer.echo(
            f"{t.token_id}  {t.label}  [{', '.join(t.scopes)}]  stw_...{t.hint}  "
            f"created {_when(t.created_at)}  last used {_when(t.last_used)}  {state}"
        )


@tokens_app.command("create")
def tokens_create(
    label: Annotated[str, typer.Argument(help="What the token is for.")],
    scope: Annotated[
        list[str] | None,
        typer.Option("--scope", help="read or propose; repeat for both. Default: read."),
    ] = None,
    expires_days: Annotated[
        int | None, typer.Option(min=1, help="Expire after this many days.")
    ] = None,
) -> None:
    """Create an API token for the owner. It's printed once; store it somewhere safe.
    No token can approve proposals, open locked folders or change settings."""
    runtime = _ready_runtime()
    auth = runtime.auth
    owner = _owner(auth, None)
    now = utcnow()
    expires_at = now + timedelta(days=expires_days) if expires_days else None
    with _auth_errors():
        info, secret = auth.issue_token(
            owner.user_id, label, scope or ["read"], actor=CLI_ACTOR, expires_at=expires_at, now=now
        )
    typer.echo(f"token {info.token_id} ({', '.join(info.scopes)}): {secret}")
    typer.echo("This is the only time the token is shown.", err=True)


@tokens_app.command("revoke")
def tokens_revoke(token_id: int) -> None:
    """Revoke an API token; it stops working immediately."""
    runtime = _ready_runtime()
    if not runtime.auth.revoke_token(token_id, actor=CLI_ACTOR):
        _fail(f"no active token {token_id}")
    typer.echo(f"token {token_id} revoked")


# --- privacy, search, journal, backups -----------------------------------------------


@files_app.command("sensitivity")
def files_sensitivity(
    path: Annotated[str, typer.Argument(help="library:path/to/file")],
    level: Annotated[str, typer.Argument(help="local-only, excluded, or clear")],
) -> None:
    """Raise one file above its folder's sensitivity (or clear that). `excluded` files
    are never read; the next scan drops their content hash."""
    if level not in ("local-only", "excluded", "clear"):
        _fail("level must be local-only, excluded or clear")
    runtime = _ready_runtime()
    library_id, _, relpath = path.partition(":")
    with runtime.dbs.state.write() as conn:
        file_id = conn.execute(
            select(files_table.c.file_id).where(
                files_table.c.library_id == library_id, files_table.c.relpath == relpath
            )
        ).scalar_one_or_none()
        if file_id is None:
            _fail(f"no known file {path} (run `steward scan` first?)")
        changed = set_override(
            conn,
            file_id,
            None if level == "clear" else level,  # type: ignore[arg-type]
            actor=CLI_ACTOR,
        )
        if changed:
            enqueue_scan(conn, library_id, [relpath])
    typer.echo(f"{path}: {level}" if changed else f"{path}: unchanged")


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Words that must all appear (path or text).")],
    library: Annotated[str | None, typer.Option(help="Only this library.")] = None,
    folder: Annotated[str | None, typer.Option(help="Only inside library:path.")] = None,
    kind: Annotated[str | None, typer.Option(help="document, image, video, audio, text...")] = None,
    limit: int = 20,
    mode: Annotated[str, typer.Option("--in", help="all (names and text), names or text.")] = "all",
    tag: Annotated[
        list[str] | None, typer.Option(help="Only files with this tag (or one under it).")
    ] = None,
) -> None:
    """Search file names, paths and extracted text (never inside locked or hidden
    folders). The query may hold filters too: tag:NAME, kind:image, amount>100."""
    if mode not in ("all", "names", "text"):
        _fail("--in must be all, names or text")
    runtime = _ready_runtime()
    with runtime.dbs.state.read() as conn:
        policy = load_policy(conn)
    try:
        results = search_all(
            runtime.dbs,
            policy,
            query,
            filters=SearchFilters(library=library, folder=folder, kind=kind, tags=tuple(tag or ())),
            limit=limit,
            mode=cast(Mode, mode),
            embed=runtime.query_embedder,
        )
    except SearchError as exc:
        _fail(str(exc))
    if results.semantic is False:
        typer.echo("(the embedding model can't be reached: searched by words only)", err=True)
    for hit in results.hits:
        meaning = "  [by meaning]" if hit.semantic else ""
        typer.echo(f"{hit.library_id}:{hit.relpath}  {hit.size} B  {hit.kind or '-'}{meaning}")
        if hit.snippet:
            page = f"p. {hit.page}: " if hit.page else ""
            typer.echo(f"    {page}{''.join(t if not m else f'[{t}]' for t, m in hit.snippet)}")
    more = "+" if results.capped else ""
    typer.echo(f"({len(results.hits)} shown, {results.total}{more} found)")


@journal_app.command("list")
def journal_list(limit: int = 20) -> None:
    """Applied actions, newest first."""
    runtime = _ready_runtime()
    for e in runtime.executor.entries(limit=limit):
        state = " [undone]" if e.undone_by else ""
        undo = f" (undo of {e.undo_of})" if e.undo_of else ""
        typer.echo(f"{e.entry_id}  {_when(e.applied_at)}  {e.actor}  {e.summary}{undo}{state}")


@journal_app.command("undo")
def journal_undo(entry_id: int) -> None:
    """Undo an applied action (refused if the files changed since). The host is trusted,
    so sensitive actions need no password here."""
    runtime = _ready_runtime()
    try:
        entry = runtime.executor.undo(entry_id, actor=CLI_ACTOR, confirmed=True)
    except (ActionRefused, ActionFailed) as exc:
        _fail(str(exc))
    typer.echo(f"{entry.entry_id}: {entry.summary}")


# --- tags ------------------------------------------------------------------------


@tags_app.command("list")
def tags_list() -> None:
    """The vocabulary with how many files (outside locked folders) have each tag."""
    runtime = _ready_runtime()
    with runtime.dbs.state.read() as conn:
        vocabulary = tag_store.vocabulary(conn)
        policy = load_policy(conn)
    counts = tag_counts(runtime.dbs, policy)
    for t in vocabulary:
        status = f"  [{t.status}]" if t.status != "active" else ""
        aliases = f"  (also {', '.join(t.aliases)})" if t.aliases else ""
        typer.echo(f"{t.name}  {counts.get(t.name, 0)}{status}{aliases}")
        if t.description:
            typer.echo(f"    {t.description}")
    if not vocabulary:
        typer.echo("no tags yet (steward tags import FILE, or steward tags bootstrap)")


def _retag(runtime: Runtime, *, everything: bool) -> int:
    stage = tag_stage(runtime.stages)
    if stage is None:
        _fail("no stage chooses tags (enable the tags module and map the apply_tags role)")
    return rerun(runtime.dbs, stage, statuses=None if everything else ("skipped",))


@tags_app.command("import")
def tags_import(
    file: Annotated[Path, typer.Argument(help="YAML: tags: [{name, description, aliases}]")],
    retag: Annotated[bool, typer.Option(help="Then have every file tagged again.")] = False,
) -> None:
    """Add the tags in a YAML file (only adds: nothing is renamed or removed)."""
    runtime = _ready_runtime()
    try:
        parsed = tag_io.parse(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, TagError) as exc:
        _fail(f"{file}: {exc}")
    with runtime.dbs.state.write() as conn:
        result = tag_io.import_tags(conn, parsed, actor=CLI_ACTOR)
    for problem in result.problems:
        typer.echo(f"skipped {problem}", err=True)
    typer.echo(
        f"{len(result.created)} created, {len(result.described)} described, "
        f"{len(result.aliases)} alias(es) added"
    )
    if retag:
        typer.echo(f"{_retag(runtime, everything=True)} result(s) dropped; tagging runs again")


@tags_app.command("export")
def tags_export(
    output: Annotated[Path | None, typer.Option(help="Write here instead of printing.")] = None,
) -> None:
    """The vocabulary as YAML that `steward tags import` reads (deprecated tags left out)."""
    runtime = _ready_runtime()
    with runtime.dbs.state.read() as conn:
        text = tag_io.export_tags(conn)
    if output is None:
        typer.echo(text, nl=False)
    else:
        output.write_text(text, encoding="utf-8")
        typer.echo(f"wrote {output}")


@tags_app.command("bootstrap")
def tags_bootstrap(
    sample: Annotated[int, typer.Option(min=5, max=1000, help="Files to learn from.")] = 60,
    max_tags: Annotated[int, typer.Option("--max", min=5, max=200, help="Most tags.")] = 40,
    output: Annotated[
        Path | None, typer.Option(help="Default: <data dir>/bootstrap/tags.yaml")
    ] = None,
) -> None:
    """Ask the generate_tags model for a first vocabulary from samples of your files
    (never locked, hidden or excluded ones). Writes YAML to edit, then import."""
    runtime = _ready_runtime()
    with runtime.dbs.state.read() as conn:
        policy = load_policy(conn)
    try:
        found = tag_bootstrap.suggest(
            runtime.dbs,
            policy,
            runtime.gateway,
            samples=sample,
            max_tags=max_tags,
            progress=lambda text: typer.echo(text, err=True),
        )
    except (tag_bootstrap.BootstrapError, ModelUnavailable, ValueError) as exc:
        _fail(str(exc))
    target = output or runtime.config.data_dir / "bootstrap" / "tags.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(tag_io.dump(found), encoding="utf-8")
    typer.echo(f"{len(found.tags)} tag(s) suggested: {target}")
    typer.echo(f"edit it, then: steward tags import {target}")


def _tag_file(action: str, path: str, tag: str) -> None:
    runtime = _ready_runtime()
    try:
        entry = runtime.executor.run(
            action, {"path": path, "tag": tag}, actor=CLI_ACTOR, confirmed=True
        )
    except (ActionRefused, ActionFailed) as exc:
        _fail(str(exc))
    typer.echo(f"{entry.entry_id}: {entry.summary}")


@tags_app.command("add")
def tags_add(
    path: Annotated[str, typer.Argument(help="library:path of a file")],
    tag: Annotated[str, typer.Argument(help="Tag name (created if it's new)")],
) -> None:
    """Tag a file (journaled; `steward journal undo` reverts it)."""
    _tag_file("tag.add", path, tag)


@tags_app.command("remove")
def tags_remove(
    path: Annotated[str, typer.Argument(help="library:path of a file")],
    tag: Annotated[str, typer.Argument(help="Tag name")],
) -> None:
    """Remove a tag from a file; it stays removed even if the AI or the folder type
    would give it."""
    _tag_file("tag.remove", path, tag)


@tags_app.command("retag")
def tags_retag(
    all_files: Annotated[
        bool, typer.Option("--all", help="Every file, not only the ones skipped.")
    ] = False,
) -> None:
    """Have tags chosen again (in the background, by `steward serve`/`worker`)."""
    runtime = _ready_runtime()
    typer.echo(f"{_retag(runtime, everything=all_files)} result(s) dropped; tagging runs again")


@app.command("backup")
def backup_command(
    keep: Annotated[
        int | None, typer.Option(help="Backups to keep (default from steward.yaml).")
    ] = None,
) -> None:
    """Write consistent copies of the databases plus state.json to the backup folder.
    Use it as an external backup tool's pre-backup hook."""
    runtime = _ready_runtime()
    try:
        info = backup.backup(runtime, keep=keep)
    except (backup.BackupError, OSError) as exc:
        _fail(str(exc))
    total = sum(info.files.values())
    typer.echo(f"{info.path} ({total} bytes)")


@app.command("export")
def export_command(
    output: Annotated[Path | None, typer.Option(help="Write here instead of stdout.")] = None,
) -> None:
    """Everything that can't be rebuilt (folder settings, files, journal, settings...) as
    JSON. Password hashes and token secrets are left out."""
    runtime = _ready_runtime()
    try:
        data = backup.export_state(runtime)
    except backup.BackupError as exc:
        _fail(str(exc))
    text = json.dumps(data, indent=1, sort_keys=True)
    if output is None:
        typer.echo(text)
    else:
        output.write_text(text)
        output.chmod(0o600)
        typer.echo(f"wrote {output}", err=True)
