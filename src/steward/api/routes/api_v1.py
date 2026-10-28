# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`/api/v1`: the JSON API over the same services the UI uses (plan §14.3).

Reading works with a session or an API token with the `read` scope. Changing anything
(running actions, undo, retrying jobs) needs a logged-in session (plus its CSRF token in
`X-CSRF-Token`), and sensitive steps a recent password check; **no token can change
files, approve anything or see inside locked folders**. Locked, hidden and trash folders
are left out of every answer (`404` for a path inside one), except search with
`locked=true` from a session that recently re-entered its password.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy import func, select

import steward
from steward.api.security import Refused, access, api_state, require_principal
from steward.core import queue
from steward.core.auth import Principal
from steward.core.db.schema import blobs, files
from steward.core.executor import ActionFailed, JournalEntry, SensitiveActionError
from steward.core.folders import FolderRef, PathError, load_policy
from steward.core.folders.policy import Policy
from steward.core.runtime import Runtime
from steward.core.search import (
    MAX_LIMIT,
    SearchError,
    SearchFilters,
    Visibility,
    search_files,
)
from steward.sdk import ActionRefused

router = APIRouter(prefix="/api/v1")
LOCKED_SUMMARY = "An action in a locked folder"


def _runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


def _reader(request: Request) -> Principal:
    principal = require_principal(request)
    if not principal.can("read"):
        raise Refused(403, "this token doesn't have the read scope")
    return principal


def _policy(runtime: Runtime) -> Policy:
    with runtime.dbs.state.read() as conn:
        return load_policy(conn)


def _confirmed(request: Request, principal: Principal) -> bool:
    """Run a sensitive step only after a recent password check (raises `ReauthRequired`,
    a 403 here, otherwise)."""
    api_state(request).auth.require_recent_auth(principal)
    return True


def _refused(exc: Exception, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=status)


# --- libraries, folders, files ---------------------------------------------------------


@router.get("/libraries")
@access("principal")
def libraries(request: Request) -> Response:
    _reader(request)
    policy = _policy(_runtime(request))
    return JSONResponse(
        {
            "libraries": [
                {"library_id": lib.library_id, "name": lib.name}
                for lib in sorted(policy.libraries.values(), key=lambda lib: lib.position)
            ]
        }
    )


@router.get("/folders")
@access("principal")
def folder(request: Request, path: Annotated[str, Query(max_length=4096)]) -> Response:
    """A folder's effective settings and its visible subfolders."""
    _reader(request)
    runtime = _runtime(request)
    policy = _policy(runtime)
    visibility = Visibility(policy)
    try:
        ref = FolderRef.parse(path)
        settings = policy.settings_for(ref)
    except PathError:
        return _refused(Refused(404, "no such folder"), 404)
    if visibility.is_hidden(ref.library_id, ref.relpath):
        return _refused(Refused(404, "no such folder"), 404)
    root = policy.library(ref.library_id).root
    subfolders: list[str] = []
    try:
        with os.scandir(f"{root}/{ref.relpath}" if ref.relpath else root) as it:
            for entry in it:
                child = f"{ref.relpath}/{entry.name}" if ref.relpath else entry.name
                if entry.is_dir(follow_symlinks=False) and not visibility.is_hidden(
                    ref.library_id, child
                ):
                    subfolders.append(entry.name)
    except OSError:
        return _refused(Refused(404, "no such folder"), 404)
    return JSONResponse(
        {
            "path": str(ref),
            "type": settings.type.name,
            "type_from": settings.type_from,
            "index": settings.index,
            "sensitivity": settings.sensitivity,
            "writable": settings.writable,
            "shareable": settings.shareable,
            "notes": list(settings.notes),
            "subfolders": sorted(subfolders),
        }
    )


def _file_json(row: Any, blob: Any) -> dict[str, Any]:
    return {
        "file_id": row.file_id,
        "path": f"{row.library_id}:{row.relpath}",
        "size": row.size,
        "modified_ns": row.mtime_ns,
        "content_hash": row.content_hash,
        "present": bool(row.present),
        "mime": blob.mime if blob is not None else None,
        "kind": blob.kind if blob is not None else None,
        "first_seen": row.first_seen.isoformat(),
    }


@router.get("/files")
@access("principal")
def list_files(
    request: Request,
    path: Annotated[str, Query(max_length=4096)],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    """Files directly inside a folder."""
    _reader(request)
    runtime = _runtime(request)
    policy = _policy(runtime)
    try:
        ref = FolderRef.parse(path)
        policy.library(ref.library_id)
    except PathError:
        return _refused(Refused(404, "no such folder"), 404)
    if Visibility(policy).is_hidden(ref.library_id, ref.relpath):
        return _refused(Refused(404, "no such folder"), 404)
    prefix = f"{ref.relpath}/" if ref.relpath else ""
    query = (
        select(files)
        .where(
            files.c.library_id == ref.library_id,
            files.c.present.is_(True),
            func.substr(files.c.relpath, 1, len(prefix)) == prefix,
            func.instr(func.substr(files.c.relpath, len(prefix) + 1), "/") == 0,
        )
        .order_by(files.c.relpath)
        .limit(limit)
        .offset(offset)
    )
    with runtime.dbs.state.read() as conn:
        rows = conn.execute(query).all()
    found = _blobs(runtime, [r.content_hash for r in rows if r.content_hash])
    return JSONResponse(
        {"path": str(ref), "files": [_file_json(r, found.get(r.content_hash)) for r in rows]}
    )


def _blobs(runtime: Runtime, hashes: list[str]) -> dict[str, Any]:
    if not hashes:
        return {}
    with runtime.dbs.index.read() as conn:
        return {
            b.content_hash: b
            for b in conn.execute(select(blobs).where(blobs.c.content_hash.in_(hashes)))
        }


@router.get("/files/{file_id}")
@access("principal")
def get_file(request: Request, file_id: str) -> Response:
    _reader(request)
    runtime = _runtime(request)
    with runtime.dbs.state.read() as conn:
        row = conn.execute(select(files).where(files.c.file_id == file_id)).one_or_none()
    if row is None or Visibility(_policy(runtime)).is_hidden(row.library_id, row.relpath):
        return _refused(Refused(404, "no such file"), 404)
    blob = _blobs(runtime, [row.content_hash] if row.content_hash else [])
    return JSONResponse(_file_json(row, blob.get(row.content_hash)))


# --- search ----------------------------------------------------------------------------


@router.get("/search")
@access("principal")
def search(
    request: Request,
    q: Annotated[str, Query(max_length=2000)] = "",
    library: Annotated[str | None, Query(max_length=64)] = None,
    folder: Annotated[str | None, Query(max_length=4096)] = None,
    kind: Annotated[str | None, Query(max_length=32)] = None,
    modified_after: datetime | None = None,
    modified_before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    locked: bool = False,
) -> Response:
    """Search file names and paths. `locked=true` searches only the locked folders and
    needs a session with a recent password check."""
    principal = _reader(request)
    if locked:
        if not principal.is_session:
            raise Refused(403, "API tokens can't reach locked folders")
        _confirmed(request, principal)
    runtime = _runtime(request)
    try:
        results = search_files(
            runtime.dbs,
            _policy(runtime),
            q,
            filters=SearchFilters(
                library=library,
                folder=folder,
                kind=kind,
                modified_after=modified_after,
                modified_before=modified_before,
            ),
            scope="locked" if locked else "normal",
            limit=limit,
            offset=offset,
        )
    except SearchError as exc:
        return _refused(exc)
    return JSONResponse(
        {
            "query": q,
            "total": results.total,
            "capped": results.capped,
            "hits": [h.as_dict() for h in results.hits],
        }
    )


# --- actions and the journal ---------------------------------------------------------


def entry_json(entry: JournalEntry, visibility: Visibility, *, show_locked: bool) -> dict[str, Any]:
    locked = any(visibility.path_locked(p) for p in entry.paths)
    redact = locked and not show_locked
    return {
        "entry_id": entry.entry_id,
        "action": entry.action,
        "summary": LOCKED_SUMMARY if redact else entry.summary,
        "locked": locked,
        "actor": entry.actor,
        "sensitive": entry.sensitive,
        "applied_at": entry.applied_at.isoformat(),
        "undoable": entry.undoable,
        "undo_of": entry.undo_of,
        "undone_by": entry.undone_by,
    }


@router.get("/actions")
@access("principal")
def list_actions(request: Request) -> Response:
    _reader(request)
    actions = _runtime(request).executor.actions
    return JSONResponse(
        {
            "actions": [
                {
                    "name": name,
                    "sensitive": a.sensitive,
                    "ai_mode": a.ai_mode,
                    "params": a.params.model_json_schema(),
                }
                for name, a in sorted(actions.items())
            ]
        }
    )


@router.post("/actions/{name}/preview")
def preview_action(
    request: Request, name: str, params: Annotated[dict[str, Any], Body()]
) -> Response:
    """What an action would do, without doing it (session only)."""
    require_principal(request)
    try:
        preview = _runtime(request).executor.preview(name, params)
    except ActionRefused as exc:
        return _refused(exc)
    return JSONResponse(
        {
            "summary": preview.rendered.summary,
            "details": list(preview.rendered.details),
            "sensitive": preview.sensitive,
        }
    )


@router.post("/actions/{name}")
def run_action(request: Request, name: str, params: Annotated[dict[str, Any], Body()]) -> Response:
    """Run an action as the logged-in owner (session only; sensitive ones need a recent
    password check, otherwise 403 with `reauth` in the message)."""
    principal = require_principal(request)
    executor = _runtime(request).executor
    try:
        try:
            entry = executor.run(name, params, actor=principal.actor)
        except SensitiveActionError:
            entry = executor.run(
                name, params, actor=principal.actor, confirmed=_confirmed(request, principal)
            )
    except ActionRefused as exc:
        return _refused(exc, 409)
    except ActionFailed as exc:
        return _refused(exc, 500)
    visibility = Visibility(_policy(_runtime(request)))
    return JSONResponse(entry_json(entry, visibility, show_locked=True), status_code=201)


@router.get("/journal")
@access("principal")
def journal(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    before: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    principal = _reader(request)
    runtime = _runtime(request)
    visibility = Visibility(_policy(runtime))
    show_locked = principal.is_session and api_state(request).auth.is_recent(principal)
    entries = runtime.executor.entries(limit=limit, before=before)
    return JSONResponse(
        {"entries": [entry_json(e, visibility, show_locked=show_locked) for e in entries]}
    )


@router.post("/journal/{entry_id}/undo")
def undo(request: Request, entry_id: int) -> Response:
    principal = require_principal(request)
    runtime = _runtime(request)
    executor = runtime.executor
    try:
        try:
            entry = executor.undo(entry_id, actor=principal.actor)
        except SensitiveActionError:
            entry = executor.undo(
                entry_id, actor=principal.actor, confirmed=_confirmed(request, principal)
            )
    except ActionRefused as exc:
        return _refused(exc, 409)
    except ActionFailed as exc:
        return _refused(exc, 500)
    visibility = Visibility(_policy(runtime))
    return JSONResponse(entry_json(entry, visibility, show_locked=True), status_code=201)


# --- jobs and health ---------------------------------------------------------------------


@router.get("/jobs")
@access("principal")
def jobs(
    request: Request,
    status: Annotated[str | None, Query(pattern="^(queued|running|done|dead)$")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
) -> Response:
    """Queue counts by lane and status, and recent jobs. Payloads (which hold paths)
    aren't included; error messages only for sessions."""
    principal = _reader(request)
    runtime = _runtime(request)
    with runtime.dbs.state.read() as conn:
        counts = queue.counts(conn)
        recent = queue.list_jobs(conn, status=status, limit=limit)
    return JSONResponse(
        {
            "counts": [
                {"lane": lane, "status": st, "jobs": n} for (lane, st), n in sorted(counts.items())
            ],
            "jobs": [
                {
                    "job_id": j.job_id,
                    "type": j.type,
                    "lane": j.lane,
                    "status": j.status,
                    "attempts": j.attempts,
                    "max_attempts": j.max_attempts,
                    "created_at": j.created_at.isoformat(),
                    "finished_at": j.finished_at.isoformat() if j.finished_at else None,
                    "last_error": j.last_error if principal.is_session else None,
                }
                for j in recent
            ],
        }
    )


@router.post("/jobs/{job_id}/retry")
def retry_job(request: Request, job_id: int) -> Response:
    require_principal(request)
    with _runtime(request).dbs.state.write() as conn:
        retried = queue.retry(conn, job_id)
    if not retried:
        return _refused(Refused(409, "only dead jobs can be retried"), 409)
    return JSONResponse({"job_id": job_id, "status": "queued"})


@router.get("/health")
@access("principal")
async def health(request: Request) -> Response:
    _reader(request)
    from steward.api.routes.pages import run_health_checks

    results = await run_health_checks(_runtime(request))
    return JSONResponse(
        {
            "status": "fail"
            if any(r.status == "fail" for _, r in results)
            else "warn"
            if any(r.status == "warn" for _, r in results)
            else "ok",
            "checks": [
                {"name": name, "status": r.status, "detail": r.detail} for name, r in results
            ],
        }
    )


# --- the API's own description -------------------------------------------------------------


@router.get("/openapi.json")
@access("principal")
def openapi(request: Request) -> Response:
    """OpenAPI description of `/api/v1` (FastAPI's own docs routes are off, since they'd
    sit outside the login check)."""
    _reader(request)
    cached = getattr(request.app.state, "api_openapi", None)
    if cached is None:
        routes = [
            c.original_route
            for c in iter_route_contexts(request.app.routes)
            if isinstance(c.original_route, APIRoute) and (c.path or "").startswith("/api/")
        ]
        cached = json.dumps(
            get_openapi(
                title="steward-ai API",
                version=steward.__version__,
                description=__doc__,
                routes=routes,
            )
        )
        request.app.state.api_openapi = cached
    return Response(cached, media_type="application/json")
