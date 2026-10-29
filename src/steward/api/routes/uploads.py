# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`/api/v1/uploads`: resumable uploads (docs/adr/0018). Session only (with the CSRF
token in `X-CSRF-Token`); API tokens can't upload.

1. `POST /api/v1/uploads` `{"folder": "lib:path", "name": "...", "size": N}` → 201 with
   `upload_id`, `offset` (0) and `chunk_bytes`. 409 with `suggestion` if the name is
   taken ("keep both" = start again with the suggestion).
2. `PUT /api/v1/uploads/{id}` with `Upload-Offset: <offset>` and the next piece as the
   body (`application/offset+octet-stream`, at most `chunk_bytes`) → 200 with the new
   `offset`. 409 with the server's `offset` if it doesn't match.
3. `POST /api/v1/uploads/{id}/finish` (optionally `{"name": "..."}`) → 201 with the
   journal entry. 409 with `suggestion` if the name was taken meanwhile.

`GET /api/v1/uploads/{id}` tells a client where to carry on after a dropped connection;
`GET /api/v1/uploads` lists unfinished uploads; `DELETE /api/v1/uploads/{id}` cancels.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from steward.api.security import api_state, require_principal
from steward.core.auth import Principal
from steward.core.executor import ActionFailed, SensitiveActionError
from steward.core.executor.uploads import (
    OffsetMismatch,
    UploadConflict,
    UploadInfo,
    UploadNotFound,
    Uploads,
)
from steward.core.files import Browser
from steward.core.folders import load_policy
from steward.core.runtime import Runtime
from steward.sdk import ActionRefused, NotFound

router = APIRouter(prefix="/api/v1/uploads")
CHUNK_TYPES = ("application/offset+octet-stream", "application/octet-stream")


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    folder: str = Field(max_length=4096)
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0, le=2**62)
    scope: Literal["normal", "locked"] = "normal"


class FinishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=255)


def _runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


def _uploads(request: Request) -> Uploads:
    return _runtime(request).uploads


def _browser(runtime: Runtime) -> Browser:
    with runtime.dbs.state.read() as conn:
        return Browser(runtime.dbs, load_policy(conn))


def _scope_ok(request: Request, principal: Principal, scope: str) -> bool:
    """Whether the locked scope may be used now (asks for the password otherwise)."""
    if scope != "locked":
        return False
    auth = api_state(request).auth
    if not auth.is_recent(principal):
        auth.require_recent_auth(principal)
    return True


def _json(info: UploadInfo, uploads: Uploads, status: int = 200) -> JSONResponse:
    return JSONResponse({**info.as_dict(), "chunk_bytes": uploads.chunk_bytes}, status_code=status)


def _refusal(exc: Exception) -> JSONResponse:
    body: dict[str, Any] = {"error": str(exc)}
    if isinstance(exc, UploadConflict):
        body["suggestion"] = exc.suggestion
        return JSONResponse(body, status_code=409)
    if isinstance(exc, OffsetMismatch):
        body["offset"] = exc.offset
        return JSONResponse(body, status_code=409)
    if isinstance(exc, (UploadNotFound, NotFound)):
        return JSONResponse({"error": "no such upload or folder"}, status_code=404)
    if isinstance(exc, SensitiveActionError):
        return JSONResponse(body, status_code=403)
    if isinstance(exc, ActionFailed):
        return JSONResponse(body, status_code=500)
    return JSONResponse(body, status_code=400)


@router.post("")
def start(request: Request, body: Annotated[StartBody, Body()]) -> Response:
    principal = require_principal(request)
    runtime = _runtime(request)
    confirmed = _scope_ok(request, principal, body.scope)
    try:
        where = _browser(runtime).locate(body.folder, body.scope)
        info = runtime.uploads.start(
            where.path, body.name, body.size, actor=principal.actor, confirmed=confirmed
        )
    except (ActionRefused, NotFound) as exc:
        return _refusal(exc)
    return _json(info, runtime.uploads, 201)


@router.get("")
def list_open(request: Request, scope: Literal["normal", "locked"] = "normal") -> Response:
    principal = require_principal(request)
    runtime = _runtime(request)
    _scope_ok(request, principal, scope)
    browser = _browser(runtime)
    found = [
        u
        for u in runtime.uploads.list_open(actor=principal.actor)
        if browser.visible(u.library_id, u.folder, scope)
    ]
    return JSONResponse(
        {"uploads": [u.as_dict() for u in found], "chunk_bytes": runtime.uploads.chunk_bytes}
    )


@router.get("/{upload_id}")
def status(request: Request, upload_id: str) -> Response:
    principal = require_principal(request)
    uploads = _uploads(request)
    try:
        return _json(uploads.get(upload_id, actor=principal.actor), uploads)
    except ActionRefused as exc:
        return _refusal(exc)


@router.put("/{upload_id}")
async def put_chunk(request: Request, upload_id: str) -> Response:
    principal = require_principal(request)
    uploads = _uploads(request)
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in CHUNK_TYPES:
        return JSONResponse(
            {"error": "send the bytes as application/offset+octet-stream"}, status_code=415
        )
    raw_offset = request.headers.get("upload-offset", "")
    if not raw_offset.isdigit():
        return JSONResponse({"error": "the Upload-Offset header is missing"}, status_code=400)
    limit = uploads.chunk_bytes
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return JSONResponse({"error": f"chunks are at most {limit} bytes"}, status_code=413)
    data = bytearray()
    try:
        async for piece in request.stream():
            data.extend(piece)
            if len(data) > limit:
                return JSONResponse({"error": f"chunks are at most {limit} bytes"}, status_code=413)
    except ClientDisconnect:
        # The connection dropped mid-chunk: nothing is written, the client resumes.
        return Response(status_code=400)
    try:
        info = await run_in_threadpool(
            uploads.write, upload_id, int(raw_offset), bytes(data), actor=principal.actor
        )
    except ActionRefused as exc:
        return _refusal(exc)
    return _json(info, uploads)


@router.post("/{upload_id}/finish")
def finish(
    request: Request, upload_id: str, body: Annotated[FinishBody | None, Body()] = None
) -> Response:
    principal = require_principal(request)
    uploads = _uploads(request)
    try:
        entry = uploads.finish(
            upload_id, actor=principal.actor, name=body.name if body is not None else None
        )
    except (ActionRefused, ActionFailed) as exc:
        return _refusal(exc)
    info = uploads.get(upload_id, actor=principal.actor)
    return JSONResponse(
        {**info.as_dict(), "entry_id": entry.entry_id, "summary": entry.summary},
        status_code=201,
    )


@router.delete("/{upload_id}")
def cancel(request: Request, upload_id: str) -> Response:
    principal = require_principal(request)
    try:
        _uploads(request).cancel(upload_id, actor=principal.actor)
    except ActionRefused as exc:
        return _refusal(exc)
    return Response(status_code=204)
