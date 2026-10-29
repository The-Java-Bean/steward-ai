# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The core's side of `steward.sdk.shares`: the handle a share-link provider gets for a
request to `/s/<token>…` (docs/adr/0020).

The provider decides *which* item a token stands for and whether its password was
given; the core decides whether that item may be shared right now and keeps every
answer inside it (`PublicItem` reads through a `Browser` in the `shared` scope)."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime
from typing import Any

from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select

from steward.api.templating import redirect, render
from steward.api.web import disposition, file_response
from steward.core.db import utcnow
from steward.core.db.schema import files
from steward.core.executor.guarded import check_segments, split_path
from steward.core.files import Browser, SharedArea, stream_zip
from steward.core.folders import load_policy
from steward.core.privacy.sharing import SharingRules
from steward.core.runtime import Runtime
from steward.sdk import ActionRefused, Crumb, Entry, EntryKind, NotFound, SharedListing
from steward.sdk.state import StateAccess

PUBLIC_HEADERS = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
}
"""On every answer under `/s/`: nothing cached, the token never leaves in a Referer,
no search engine indexes it."""


def _join(base: str, sub: str) -> str:
    if not sub:
        return base
    return f"{base}/{sub}" if base else sub


class PublicItem:
    """A `SharedItem`: one checked file or folder."""

    def __init__(self, request: Request, browser: Browser, area: SharedArea, entry: Entry) -> None:
        self.request = request
        self.browser = browser
        self.area = area
        self._entry = entry

    @property
    def name(self) -> str:
        return self._entry.name

    @property
    def kind(self) -> EntryKind:
        return self._entry.kind

    def _path(self, sub: str) -> str:
        if sub and self.kind == "file":
            raise NotFound(sub)
        try:
            check_segments(sub)
        except ActionRefused:
            raise NotFound(sub) from None
        return f"{self.area.library_id}:{_join(self.area.relpath, sub)}"

    def _relative(self, entry: Entry) -> Entry:
        relpath = entry.path.partition(":")[2]
        sub = relpath[len(self.area.relpath) :].lstrip("/") if self.area.relpath else relpath
        return Entry(
            name=entry.name,
            path=sub,
            kind=entry.kind,
            size=entry.size,
            modified=entry.modified,
            mime=entry.mime,
            file_kind=entry.file_kind,
        )

    def entry(self, sub: str = "") -> Entry:
        return self._relative(self.browser.entry(self._path(sub), scope="shared"))

    def listing(self, sub: str = "", *, limit: int = 2000) -> SharedListing:
        if self.kind != "dir":
            raise NotFound(sub)
        found = self.browser.listing(self._path(sub), scope="shared", limit=limit)
        parts = sub.split("/") if sub else []
        crumbs = [Crumb(self.name, "")]
        crumbs += [Crumb(parts[i - 1], "/".join(parts[:i])) for i in range(1, len(parts))]
        return SharedListing(
            sub=sub,
            name=parts[-1] if parts else self.name,
            crumbs=tuple(crumbs) if parts else (),
            entries=tuple(self._relative(e) for e in found.entries),
            truncated=found.truncated,
        )

    def read_text(self, sub: str = "", *, max_bytes: int) -> tuple[str, bool]:
        with self.browser.open(self._path(sub), scope="shared") as f:
            data = f.pread(max_bytes + 1, 0)
        return data[:max_bytes].decode("utf-8", errors="replace"), len(data) > max_bytes

    def file_response(self, sub: str = "", *, inline: bool = False) -> Response:
        opened = self.browser.open(self._path(sub), scope="shared")
        try:
            return file_response(opened, self.request.headers.get("range"), inline=inline)
        except BaseException:
            opened.close()
            raise

    def zip_response(self, sub: str = "", *, name: str) -> Response:
        path = self._path(sub)
        if self.browser.entry(path, scope="shared").kind != "dir":
            raise NotFound(sub)
        members = self.browser.zip_members([path], scope="shared")
        filename = name if name.lower().endswith(".zip") else f"{name}.zip"
        return StreamingResponse(
            stream_zip(members),
            media_type="application/zip",
            headers={"Content-Disposition": disposition("attachment", filename)},
        )


class ShareHandle:
    """A `ShareRequest` (see `steward.sdk.shares`)."""

    def __init__(
        self,
        request: Request,
        *,
        module: str,
        token: str,
        subpath: str,
        form: Mapping[str, str],
    ) -> None:
        self.request = request
        self.module = module
        self._token = token
        self._subpath = subpath
        self._form = dict(form)

    @property
    def runtime(self) -> Runtime:
        runtime: Runtime = self.request.app.state.runtime
        return runtime

    @property
    def token(self) -> str:
        return self._token

    @property
    def subpath(self) -> str:
        return self._subpath

    @property
    def method(self) -> str:
        return self.request.method

    @property
    def query(self) -> Mapping[str, str]:
        return dict(self.request.query_params)

    @property
    def form(self) -> Mapping[str, str]:
        return self._form

    @property
    def client_ip(self) -> str:
        ip: str = self.request.state.ip
        return ip

    @property
    def user_agent(self) -> str:
        return self.request.headers.get("user-agent", "")[:300]

    @property
    def state(self) -> StateAccess:
        return self.runtime.dbs.state

    @property
    def form_token(self) -> str:
        token: str = getattr(self.request.state, "csrf", None) or self.request.state.presession
        return token

    @property
    def base_url(self) -> str:
        return f"/s/{self._token}"

    def now(self) -> datetime:
        return utcnow()

    def cookie(self, name: str) -> str | None:
        return self.request.cookies.get(name)

    def open(
        self, path: str, kind: EntryKind, *, only_files: Collection[str] | None = None
    ) -> PublicItem:
        try:
            library_id, relpath = split_path(path)
            check_segments(relpath)
        except ActionRefused:
            raise NotFound(path) from None
        if relpath == "":
            raise NotFound(path)  # whole libraries are never shared
        dbs = self.runtime.dbs
        with dbs.state.read() as conn:
            policy = load_policy(conn)
            sharing = SharingRules.load(conn, policy, self.runtime.privacy)
            allowed = (
                _snapshot(conn, library_id, relpath, only_files) if only_files is not None else None
            )
        if not sharing.check(library_id, relpath).shareable:
            raise NotFound(path)
        area = SharedArea(library_id, relpath, sharing, allowed)
        browser = Browser(dbs, policy, shared=area)
        entry = browser.entry(path, scope="shared")
        if entry.kind != kind:
            raise NotFound(path)
        return PublicItem(self.request, browser, area, entry)

    def render(
        self,
        template: str,
        context: Mapping[str, object] | None = None,
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        ctx: dict[str, Any] = {
            "share_base": self.base_url,
            **(context or {}),
            "principal": None,
            "nav": [],
        }
        return render(
            self.request,
            f"{self.module}/{template}",
            ctx,
            status=status,
            headers={**PUBLIC_HEADERS, **(headers or {})},
        )

    def redirect(self, url: str) -> Response:
        base = self.base_url
        target = url if url == base or url.startswith((base + "/", base + "?")) else base
        response = redirect(target)
        response.headers.update(PUBLIC_HEADERS)
        return response

    def set_cookie(self, response: Response, name: str, value: str, *, max_age: int) -> None:
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            path=self.base_url,
            httponly=True,
            secure=bool(getattr(self.request.state, "https", False)),
            samesite="lax",
        )

    def not_found(self) -> Response:
        return not_found(self.request)


def not_found(request: Request) -> Response:
    return render(
        request,
        "share_missing.html",
        {"principal": None, "nav": []},
        status=404,
        headers=dict(PUBLIC_HEADERS),
    )


def _snapshot(conn: Any, library_id: str, root: str, file_ids: Collection[str]) -> frozenset[str]:
    """The relpaths a snapshot reaches now: its files still inside the item, and the
    folders on the way to them."""
    wanted = list(dict.fromkeys(file_ids))
    allowed: set[str] = {root}
    prefix = root + "/"
    for start in range(0, len(wanted), 500):
        rows = conn.execute(
            select(files.c.relpath).where(
                files.c.file_id.in_(wanted[start : start + 500]),
                files.c.library_id == library_id,
                files.c.present.is_(True),
            )
        )
        for row in rows:
            relpath: str = row.relpath
            if not relpath.startswith(prefix):
                continue
            allowed.add(relpath)
            parts = relpath.split("/")
            for i in range(len(root.split("/")), len(parts)):
                allowed.add("/".join(parts[:i]))
    return frozenset(allowed)
