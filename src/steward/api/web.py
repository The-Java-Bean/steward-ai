# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The core's side of `steward.sdk.web`: the per-request handle module pages get.

`WebHandle` is built by `make_web` (installed as `app.state.steward_web`) for requests
to a module page; it works for whoever the guard let in (always a browser session, the
only level module pages have).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from steward.api.security import api_state, require_principal, safe_next
from steward.api.templating import redirect, render
from steward.core.auth import Principal
from steward.core.executor import ActionFailed, Executor, SensitiveActionError
from steward.core.executor.guarded import check_segments, split_path
from steward.core.files import READ_CHUNK, Browser, OpenFile, stream_zip
from steward.core.folders import load_policy
from steward.core.privacy.sharing import HIDDEN, SharingRules
from steward.core.runtime import Runtime
from steward.sdk import ActionRefused, PathPrivacy, StateAccess
from steward.sdk.web import (
    ActionDone,
    ActionPreview,
    Entry,
    EntryKind,
    FilePage,
    FolderListing,
    LibraryInfo,
    LockedFolder,
    NotFound,
    Scope,
    StepOutcome,
    UnlockPreview,
    Viewer,
)

INLINE_TYPES = re.compile(
    r"^(image/(png|jpeg|gif|webp|avif|bmp)|audio/.+|video/.+|application/pdf|text/plain)$"
)
"""Shown in the browser by `file_response(inline=True)`; everything else downloads."""

RAW_CSP = "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; sandbox"
"""User content can't run scripts or load anything, even when opened directly."""
PDF_CSP = (
    "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; frame-ancestors 'self'"
)
"""Browsers' PDF viewers refuse sandboxed documents; PDFs may be framed by our pages."""

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


@dataclass(frozen=True, slots=True)
class PageInfo:
    module: str
    path: str
    templates: bool
    static: bool


def page_for(request: Request) -> PageInfo:
    pages: list[PageInfo] = request.app.state.module_pages
    path = request.url.path
    for page in pages:  # longest first
        if path == page.path or path.startswith(page.path + "/"):
            return page
    raise RuntimeError("steward web services are only for module pages")


def make_web(request: Request) -> WebHandle:
    return WebHandle(request, page_for(request))


class WebHandle:
    def __init__(self, request: Request, page: PageInfo) -> None:
        self.request = request
        self.page = page
        self.principal: Principal = require_principal(request)

    @property
    def runtime(self) -> Runtime:
        runtime: Runtime = self.request.app.state.runtime
        return runtime

    @property
    def viewer(self) -> Viewer:
        return Viewer(self.principal.user.username, self.principal.actor)

    @property
    def module(self) -> str:
        return self.page.module

    @cached_property
    def files(self) -> WebFiles:
        return WebFiles(self)

    @cached_property
    def actions(self) -> WebActions:
        return WebActions(self)

    @property
    def csrf_token(self) -> str:
        token: str = self.request.state.csrf
        return token

    @property
    def static_url(self) -> str:
        return f"/static/m/{self.module}"

    @property
    def state(self) -> StateAccess:
        return self.runtime.dbs.state

    def page_url(self, name: str) -> str | None:
        pages: dict[str, str] = self.request.app.state.page_paths
        return pages.get(name)

    def link_to(self, path: str, kind: EntryKind) -> str | None:
        return first_link(self.request, path, kind)

    def absolute_url(self, path: str) -> str:
        return absolute_url(self.request, path)

    def render(
        self,
        template: str,
        context: Mapping[str, Any] | None = None,
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        if not self.page.templates:
            raise RuntimeError(f"module {self.module!r} registered no templates")
        ctx = {"static_url": self.static_url, **(context or {})}
        return render(
            self.request,
            f"{self.module}/{template}",
            ctx,
            status=status,
            headers=dict(headers) if headers else None,
        )

    def redirect(self, url: str) -> Response:
        return redirect(safe_next(url))

    def recently_authenticated(self) -> bool:
        return api_state(self.request).auth.is_recent(self.principal)

    def require_reauth(self) -> None:
        api_state(self.request).auth.require_recent_auth(self.principal)

    def browser(self) -> Browser:
        with self.runtime.dbs.state.read() as conn:
            policy = load_policy(conn)
        return Browser(self.runtime.dbs, policy)

    def check_scope(self, scope: Scope) -> None:
        """The locked scope needs a recent password check (without using up a
        one-shot re-auth on every page view: `is_recent` first)."""
        if scope == "locked" and not self.recently_authenticated():
            self.require_reauth()


class WebFiles:
    def __init__(self, web: WebHandle) -> None:
        self.web = web

    def libraries(self) -> list[LibraryInfo]:
        return self.web.browser().libraries()

    def listing(self, path: str, *, scope: Scope = "normal", limit: int = 5000) -> FolderListing:
        self.web.check_scope(scope)
        return self.web.browser().listing(path, scope=scope, limit=limit)

    def entry(self, path: str, *, scope: Scope = "normal") -> Entry:
        self.web.check_scope(scope)
        return self.web.browser().entry(path, scope=scope)

    def by_kind(
        self,
        kind: str,
        *,
        library: str | None = None,
        scope: Scope = "normal",
        limit: int = 100,
        offset: int = 0,
    ) -> FilePage:
        self.web.check_scope(scope)
        return self.web.browser().by_kind(
            kind, library=library, scope=scope, limit=limit, offset=offset
        )

    def search(
        self,
        query: str,
        *,
        scope: Scope = "normal",
        folder: str | None = None,
        kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> FilePage:
        self.web.check_scope(scope)
        return self.web.browser().search(
            query, scope=scope, folder=folder, kind=kind, limit=limit, offset=offset
        )

    def locked_folders(self) -> list[LockedFolder]:
        self.web.check_scope("locked")
        return self.web.browser().locked_folders()

    def unlock_preview(self, path: str) -> UnlockPreview:
        self.web.check_scope("locked")
        return self.web.browser().unlock_preview(path)

    def privacy(self, paths: Sequence[str]) -> dict[str, PathPrivacy]:
        runtime = self.web.runtime
        with runtime.dbs.state.read() as conn:
            sharing = SharingRules.load(conn, load_policy(conn), runtime.privacy)
        found: dict[str, PathPrivacy] = {}
        for path in paths:
            try:
                library_id, relpath = split_path(path)
                check_segments(relpath)
            except ActionRefused:
                found[path] = HIDDEN
                continue
            found[path] = sharing.check(library_id, relpath)
        return found

    def read_text(self, path: str, *, scope: Scope = "normal", max_bytes: int) -> tuple[str, bool]:
        self.web.check_scope(scope)
        with self.web.browser().open(path, scope=scope) as f:
            data = f.pread(max_bytes + 1, 0)
        return data[:max_bytes].decode("utf-8", errors="replace"), len(data) > max_bytes

    def file_response(
        self, path: str, *, scope: Scope = "normal", inline: bool = False
    ) -> Response:
        self.web.check_scope(scope)
        opened = self.web.browser().open(path, scope=scope)
        try:
            return file_response(opened, self.web.request.headers.get("range"), inline=inline)
        except BaseException:
            opened.close()
            raise

    def zip_response(self, paths: Sequence[str], *, scope: Scope = "normal", name: str) -> Response:
        self.web.check_scope(scope)
        if not paths:
            raise NotFound("nothing to zip")
        members = self.web.browser().zip_members(paths, scope=scope)
        filename = name if name.lower().endswith(".zip") else f"{name}.zip"
        return StreamingResponse(
            stream_zip(members),
            media_type="application/zip",
            headers={"Content-Disposition": disposition("attachment", filename)},
        )


def disposition(kind: str, filename: str) -> str:
    """`Content-Disposition` with an ASCII fallback and the exact UTF-8 name."""
    fallback = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename)
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


def file_response(opened: OpenFile, range_header: str | None, *, inline: bool) -> Response:
    """Stream an open file (closed when the response ends), honoring one byte range."""
    entry = opened.entry
    size = opened.size
    mime = entry.mime or "application/octet-stream"
    show = inline and bool(INLINE_TYPES.match(mime))
    if mime.startswith("text/") and show:
        mime = "text/plain; charset=utf-8"
    headers = {
        "Content-Disposition": disposition("inline" if show else "attachment", entry.name),
        "Accept-Ranges": "bytes",
        "Content-Security-Policy": PDF_CSP if mime == "application/pdf" else RAW_CSP,
        "Cache-Control": "private, no-store",
    }
    if mime == "application/pdf":
        headers["X-Frame-Options"] = "SAMEORIGIN"
    start, end, status = 0, size - 1, 200
    if range_header:
        parsed = _parse_range(range_header, size)
        if parsed is None:
            opened.close()
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        start, end = parsed
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(max(end - start + 1, 0))
    return StreamingResponse(
        _chunks(opened, start, end), status_code=status, media_type=mime, headers=headers
    )


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    match = _RANGE.match(header.strip())
    if match is None or size == 0:
        return None
    first, last = match.groups()
    if not first and not last:
        return None
    if not first:  # the last N bytes
        n = int(last)
        if n == 0:
            return None
        return max(size - n, 0), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        return None
    return start, end


def _chunks(opened: OpenFile, start: int, end: int) -> Iterator[bytes]:
    try:
        offset = start
        while offset <= end:
            data = opened.pread(min(READ_CHUNK, end - offset + 1), offset)
            if not data:
                break
            offset += len(data)
            yield data
    finally:
        opened.close()


class WebActions:
    def __init__(self, web: WebHandle) -> None:
        self.web = web

    @property
    def executor(self) -> Executor:
        return self.web.runtime.executor

    def preview(self, name: str, params: Mapping[str, Any]) -> ActionPreview:
        p = self.executor.preview(name, params)
        return ActionPreview(p.rendered.summary, tuple(p.rendered.details), p.sensitive)

    def run(self, name: str, params: Mapping[str, Any]) -> ActionDone:
        actor = self.web.principal.actor
        try:
            entry = self.executor.run(name, params, actor=actor)
        except SensitiveActionError:
            self.web.require_reauth()
            entry = self.executor.run(name, params, actor=actor, confirmed=True)
        return ActionDone(entry.entry_id, entry.summary, entry.undoable)

    def run_all(self, steps: Sequence[tuple[str, Mapping[str, Any]]]) -> list[StepOutcome]:
        sensitive = False
        for name, params in steps:
            try:
                sensitive = sensitive or self.executor.preview(name, params).sensitive
            except ActionRefused:
                continue  # reported when the step runs
        if sensitive:
            self.web.require_reauth()
        actor = self.web.principal.actor
        outcomes: list[StepOutcome] = []
        for name, params in steps:
            try:
                entry = self.executor.run(name, params, actor=actor, confirmed=sensitive)
            except SensitiveActionError:
                # Became sensitive after the preview (a folder was locked meanwhile).
                outcomes.append(StepOutcome(name, params, error="needs your password; try again"))
            except (ActionRefused, ActionFailed) as exc:
                outcomes.append(StepOutcome(name, params, error=str(exc)))
            else:
                done = ActionDone(entry.entry_id, entry.summary, entry.undoable)
                outcomes.append(StepOutcome(name, params, done=done))
        return outcomes

    def undo(self, entry_id: int) -> ActionDone:
        actor = self.web.principal.actor
        try:
            entry = self.executor.undo(entry_id, actor=actor)
        except SensitiveActionError:
            self.web.require_reauth()
            entry = self.executor.undo(entry_id, actor=actor, confirmed=True)
        return ActionDone(entry.entry_id, entry.summary, entry.undoable)


def absolute_url(request: Request, path: str) -> str:
    """`path` as a full URL: `api.public_url` if set, else this request's address."""
    configured = request.app.state.runtime.config.steward.api.public_url
    if configured:
        return f"{configured}{path}"
    scheme = "https" if getattr(request.state, "https", False) else request.url.scheme
    return f"{scheme}://{request.url.netloc}{path}"


def first_link(request: Request, path: str, kind: str) -> str | None:
    """A link to `path` from the first module that builds them (for core pages)."""
    for builder in request.app.state.link_builders:
        try:
            url = builder(path, kind)
        except Exception:  # noqa: S112 -- a module's bug mustn't break core pages
            continue
        if url:
            return str(url)
    return None
