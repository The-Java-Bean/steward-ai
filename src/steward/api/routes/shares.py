# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`/s/<token>` and `/s/<token>/<path>`: the public share links (plan §14.4).

The only unauthenticated routes that serve files. They hand the request to the one
registered share-link handler (`app.state.share_links`); without one, or for a token
that can't be one, every link is a 404."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from steward.api.security import access
from steward.api.share import PUBLIC_HEADERS, ShareHandle, not_found
from steward.sdk import TOKEN_PATTERN, NotFound

router = APIRouter()


@router.api_route("/s/{token}", methods=["GET", "POST"])
@access("public")
async def share_root(request: Request, token: str) -> Response:
    return await _handle(request, token, "")


@router.api_route("/s/{token}/{rest:path}", methods=["GET", "POST"])
@access("public")
async def share_inside(request: Request, token: str, rest: str) -> Response:
    return await _handle(request, token, rest.strip("/"))


async def _handle(request: Request, token: str, subpath: str) -> Response:
    provider = request.app.state.share_links
    if provider is None or not TOKEN_PATTERN.match(token):
        return not_found(request)
    module, handler = provider
    form: dict[str, str] = {}
    if request.method == "POST" and request.headers.get("content-type", "").startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        data = await request.form()  # parsed (and CSRF-checked) by the guard already
        form = {k: v for k, v in data.items() if isinstance(v, str)}
    share = ShareHandle(request, module=module, token=token, subpath=subpath, form=form)

    def call() -> Response:
        try:
            response: Response = handler(share)
        except NotFound:
            return share.not_found()
        for name, value in PUBLIC_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    return await run_in_threadpool(call)
