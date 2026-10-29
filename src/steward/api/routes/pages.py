# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Core UI pages: `/ui/search`, `/ui/journal`, `/ui/jobs`, `/ui/health` and the folder
settings at `/ui/settings/folders`. Plain HTML forms (no JavaScript); every change is a
POST with the session's CSRF token, and sensitive steps redirect to `/reauth`."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from steward.api.routes.api_v1 import LOCKED_SUMMARY
from steward.api.security import api_state, require_principal
from steward.api.templating import redirect, render
from steward.api.web import first_link
from steward.core import queue, settings
from steward.core.db import utcnow
from steward.core.executor import ActionFailed, SensitiveActionError
from steward.core.folders import FolderError, FolderRef, PathError, load_policy
from steward.core.folders import store as folder_store
from steward.core.folders.types import FolderTypeError
from steward.core.runtime import Runtime
from steward.core.search import SearchError, SearchFilters, Visibility, search_files
from steward.sdk import ActionRefused, HealthResult

router = APIRouter()
KINDS = ("document", "image", "video", "audio", "text", "archive", "other")
HEALTH_TIMEOUT_SECONDS = 10
JOURNAL_PAGE = 50
STATUS_ORDER = {"fail": 0, "warn": 1, "ok": 2}


def _runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


# --- search ----------------------------------------------------------------------------


@router.get("/ui/search")
def search_page(
    request: Request,
    q: Annotated[str, Query(max_length=2000)] = "",
    kind: Annotated[str, Query(max_length=32)] = "",
    library: Annotated[str, Query(max_length=64)] = "",
    page: Annotated[int, Query(ge=1, le=1000)] = 1,
) -> Response:
    runtime = _runtime(request)
    with runtime.dbs.state.read() as conn:
        policy = load_policy(conn)
    context: dict[str, Any] = {
        "q": q,
        "kind": kind,
        "library": library,
        "kinds": KINDS,
        "libraries": sorted(policy.libraries),
        "results": None,
        "page": page,
        "per_page": 50,
        "link": lambda path: first_link(request, path, "file"),
        "folder_link": lambda path: first_link(request, path, "dir"),
    }
    if q.strip() or kind:
        try:
            context["results"] = search_files(
                runtime.dbs,
                policy,
                q,
                filters=SearchFilters(kind=kind or None, library=library or None),
                limit=50,
                offset=(page - 1) * 50,
            )
        except SearchError as exc:
            context["error"] = str(exc)
    return render(request, "search.html", context)


# --- journal -----------------------------------------------------------------------------


def _journal_context(request: Request, before: int | None) -> dict[str, Any]:
    runtime = _runtime(request)
    principal = require_principal(request)
    with runtime.dbs.state.read() as conn:
        visibility = Visibility(load_policy(conn))
    show_locked = api_state(request).auth.is_recent(principal)
    entries = runtime.executor.entries(limit=JOURNAL_PAGE, before=before)
    rows = [
        {
            "entry": e,
            "summary": e.summary
            if show_locked or not any(visibility.path_locked(p) for p in e.paths)
            else LOCKED_SUMMARY,
        }
        for e in entries
    ]
    return {
        "rows": rows,
        "show_locked": show_locked,
        "older": entries[-1].entry_id if len(entries) == JOURNAL_PAGE else None,
    }


@router.get("/ui/journal")
def journal_page(request: Request, before: Annotated[int | None, Query(ge=1)] = None) -> Response:
    return render(request, "journal.html", _journal_context(request, before))


@router.post("/ui/journal/{entry_id}/undo")
def journal_undo(request: Request, entry_id: int) -> Response:
    principal = require_principal(request)
    executor = _runtime(request).executor
    try:
        try:
            executor.undo(entry_id, actor=principal.actor)
        except SensitiveActionError:
            api_state(request).auth.require_recent_auth(principal)  # may redirect to /reauth
            executor.undo(entry_id, actor=principal.actor, confirmed=True)
    except (ActionRefused, ActionFailed) as exc:
        context = {**_journal_context(request, None), "error": str(exc)}
        return render(request, "journal.html", context, status=409)
    return redirect("/ui/journal?notice=undone")


# --- jobs ---------------------------------------------------------------------------------


@router.get("/ui/jobs")
def jobs_page(
    request: Request,
    status: Annotated[str, Query(pattern="^(|queued|running|done|dead)$")] = "",
) -> Response:
    runtime = _runtime(request)
    with runtime.dbs.state.read() as conn:
        counts = queue.counts(conn)
        jobs = queue.list_jobs(conn, status=status or None, limit=100)
        dead = queue.list_jobs(conn, status="dead", limit=100) if status != "dead" else []
    lanes = sorted({lane for lane, _ in counts})
    return render(
        request,
        "jobs.html",
        {
            "counts": counts,
            "lanes": lanes,
            "statuses": ("queued", "running", "done", "dead"),
            "jobs": jobs,
            "dead": dead,
            "status": status,
        },
    )


@router.post("/ui/jobs/{job_id}/retry")
def jobs_retry(request: Request, job_id: int) -> Response:
    with _runtime(request).dbs.state.write() as conn:
        retried = queue.retry(conn, job_id)
    return redirect("/ui/jobs?notice=" + ("job_retried" if retried else "job_not_dead"))


# --- health ---------------------------------------------------------------------------------


async def _run_check(name: str, check: Any) -> tuple[str, HealthResult]:
    try:
        result = await asyncio.wait_for(check(), timeout=HEALTH_TIMEOUT_SECONDS)
    except TimeoutError:
        return name, HealthResult("fail", "didn't answer in time")
    except Exception as exc:
        return name, HealthResult("fail", f"failed: {type(exc).__name__}")
    if not isinstance(result, HealthResult):
        return name, HealthResult("fail", "returned something that isn't a HealthResult")
    return name, result


async def run_health_checks(runtime: Runtime) -> list[tuple[str, HealthResult]]:
    """Every registered health check (core and modules), plus core state checks."""
    checks = runtime.registrations.health_checks()
    results = list(
        await asyncio.gather(*(_run_check(name, check) for name, check in checks.items()))
    )
    results.extend(await run_in_threadpool(_state_checks, runtime))
    return sorted(
        results, key=lambda item: ({"fail": 0, "warn": 1, "ok": 2}[item[1].status], item[0])
    )


def _state_checks(runtime: Runtime) -> list[tuple[str, HealthResult]]:
    found: list[tuple[str, HealthResult]] = []
    with runtime.dbs.state.read() as conn:
        counts = queue.counts(conn)
        setup = settings.get_all(conn, "setup")
        policy = load_policy(conn)
    dead = sum(n for (_, status), n in counts.items() if status == "dead")
    waiting = sum(n for (_, status), n in counts.items() if status == "queued")
    found.append(
        (
            "core.jobs",
            HealthResult(
                "warn" if dead else "ok",
                f"{waiting} queued, {dead} dead"
                + ("; retry them on the jobs page" if dead else ""),
            ),
        )
    )
    if setup.get("private_skipped") and not Visibility(policy).locked:
        found.append(
            ("core.private_folder", HealthResult("warn", "no private (locked) folder chosen"))
        )
    roles = sorted(runtime.config.llm.roles)
    found.append(
        (
            "core.models",
            HealthResult(
                "ok",
                f"roles configured: {', '.join(roles)}"
                if roles
                else "no model configured; features that need one stay off",
            ),
        )
    )
    return found


@router.get("/ui/health")
async def health_page(request: Request) -> Response:
    runtime = _runtime(request)
    results = await run_health_checks(runtime)
    auth = api_state(request).auth
    failures = await run_in_threadpool(auth.failed_attempts, utcnow() - timedelta(hours=24))
    return render(
        request,
        "health.html",
        {
            "results": results,
            "failures": failures[:50],
            "modules": runtime.modules.names(),
            "warnings": runtime.warnings,
        },
    )


# --- folder settings -------------------------------------------------------------------------


@router.get("/ui/settings/folders")
def folders_page(request: Request, error: str | None = None, status: int = 200) -> Response:
    runtime = _runtime(request)
    principal = require_principal(request)
    with runtime.dbs.state.read() as conn:
        policy = load_policy(conn)
    rows = []
    for ref, mapping in sorted(policy.mappings.items()):
        effective = policy.settings_for(ref)
        rows.append({"ref": ref, "mapping": mapping, "settings": effective})
    return render(
        request,
        "folders.html",
        {
            "libraries": sorted(policy.libraries.values(), key=lambda lib: lib.position),
            "rows": rows,
            "types": sorted(policy.types.values(), key=lambda t: (not t.builtin, t.name)),
            "roles": sorted(policy.roles.items()),
            "recent": api_state(request).auth.is_recent(principal),
            "error": error,
        },
        status=status,
    )


def _folder_ref(text: str) -> FolderRef:
    try:
        return FolderRef.parse(text.strip())
    except PathError as exc:
        raise FolderError(str(exc)) from None


@router.post("/ui/settings/folders/map")
def folders_map(
    request: Request,
    folder: Annotated[str, Form(max_length=4096)],
    type_name: Annotated[str, Form(max_length=100)],
) -> Response:
    principal = require_principal(request)
    runtime = _runtime(request)
    try:
        ref = _folder_ref(folder)
        with runtime.dbs.state.read() as conn:
            policy = load_policy(conn)
        current = policy.settings_for(ref)
        new_type = policy.types.get(type_name)
        if new_type is None:
            raise FolderError(f"unknown folder type {type_name!r}")
        # Mapping a folder to a type that unlocks it, or that's less private, counts as
        # unlocking: ask for the password again.
        if (current.locked and not new_type.locked and current.locked_by == ref.relpath) or (
            _rank(new_type.sensitivity) < _rank(current.type.sensitivity)
        ):
            api_state(request).auth.require_recent_auth(principal)
        with runtime.dbs.state.write() as conn:
            folder_store.set_type(conn, ref, type_name, actor=principal.actor)
    except (FolderError, FolderTypeError, PathError) as exc:
        return folders_page(request, error=str(exc), status=400)
    return redirect("/ui/settings/folders?notice=folder_saved")


def _rank(level: str) -> int:
    return ("normal", "local-only", "excluded").index(level)


@router.post("/ui/settings/folders/unmap")
def folders_unmap(request: Request, folder: Annotated[str, Form(max_length=4096)]) -> Response:
    principal = require_principal(request)
    runtime = _runtime(request)
    try:
        ref = _folder_ref(folder)
        with runtime.dbs.state.read() as conn:
            policy = load_policy(conn)
        mapping = policy.mappings.get(ref)
        if mapping is not None and mapping.type_name is not None:
            mapped = policy.types[mapping.type_name]
            if mapped.locked or mapped.sensitivity != "normal":
                api_state(request).auth.require_recent_auth(principal)
        with runtime.dbs.state.write() as conn:
            folder_store.set_type(conn, ref, None, actor=principal.actor)
    except (FolderError, FolderTypeError, PathError) as exc:
        return folders_page(request, error=str(exc), status=400)
    return redirect("/ui/settings/folders?notice=folder_saved")


@router.post("/ui/settings/folders/lock")
def folders_lock(
    request: Request,
    folder: Annotated[str, Form(max_length=4096)],
    locked: Annotated[bool, Form()] = True,
) -> Response:
    principal = require_principal(request)
    executor = _runtime(request).executor
    action = "folder.lock" if locked else "folder.unlock"
    try:
        ref = _folder_ref(folder)
        params = {"path": str(ref)}
        try:
            executor.run(action, params, actor=principal.actor)
        except SensitiveActionError:
            api_state(request).auth.require_recent_auth(principal)
            executor.run(action, params, actor=principal.actor, confirmed=True)
    except (FolderError, ActionRefused, ActionFailed) as exc:
        return folders_page(request, error=str(exc), status=400)
    return redirect(
        "/ui/settings/folders?notice=" + ("folder_locked" if locked else "folder_unlocked")
    )
