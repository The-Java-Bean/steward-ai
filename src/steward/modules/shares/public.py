# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The public side: what someone opening `/s/<token>` sees.

An unknown, expired or revoked token, or an item that may no longer be shared, all get
the same "isn't available" page, so a visitor learns nothing about which it was. A
password, once given, is remembered by a cookie for `GRANT_LIFETIME`. Wrong passwords
are counted per address and per share (`shares_access`) and slowed down with 429s.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import quote

from fastapi.responses import Response

from steward.modules.shares import credentials, store
from steward.sdk import Entry, NotFound, SharedItem, ShareRequest

TEXT_PREVIEW_BYTES = 256 * 1024
FAILURE_WINDOW = timedelta(minutes=15)
MAX_FAILURES_PER_ADDRESS = 10
MAX_FAILURES_PER_SHARE = 100


def preview_kind(entry: Entry) -> str | None:
    mime = entry.mime or ""
    if mime in ("image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp"):
        return "image"
    if mime == "application/pdf":
        return "pdf"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("text/") or entry.file_kind == "text":
        return "text"
    return None


def _href(share: ShareRequest, sub: str, **query: str) -> str:
    url = share.base_url + (f"/{quote(sub)}" if sub else "")
    if query:
        url += "?" + "&".join(f"{k}={quote(v)}" for k, v in query.items())
    return url


def handle(share: ShareRequest) -> Response:
    now = share.now()
    found = store.by_token(share.state, share.token)
    if found is None or not found.active(now):
        return share.not_found()
    if found.password_hash is not None:
        hashed = found.password_hash
        cookie = share.cookie(credentials.GRANT_COOKIE)
        if not credentials.check_grant(hashed, found.share_id, cookie, now):
            return _password(share, found, hashed)
    if share.method == "POST":
        return share.redirect(_href(share, share.subpath))
    only = store.snapshot_ids(share.state, found.share_id) if found.snapshot else None
    item = share.open(found.path, found.kind, only_files=only)
    return _serve(share, found, item)


def _record(share: ShareRequest, found: store.Share, kind: store.AccessKind) -> None:
    store.record(
        share.state,
        found.share_id,
        kind,
        ip=share.client_ip,
        user_agent=share.user_agent,
        now=share.now(),
    )


def _password(share: ShareRequest, found: store.Share, hashed: str) -> Response:
    context: dict[str, Any] = {"action": _href(share, share.subpath)}
    if share.method != "POST":
        return share.render("public/password.html", context, status=401)
    since = share.now() - FAILURE_WINDOW
    if (
        store.failures(share.state, found.share_id, since=since, ip=share.client_ip)
        >= MAX_FAILURES_PER_ADDRESS
        or store.failures(share.state, found.share_id, since=since, ip=None)
        >= MAX_FAILURES_PER_SHARE
    ):
        context["error"] = "Too many wrong passwords. Wait a few minutes and try again."
        return share.render(
            "public/password.html", context, status=429, headers={"Retry-After": "900"}
        )
    if credentials.verify_password(hashed, share.form.get("password", "")):
        response = share.redirect(_href(share, share.subpath))
        grant = credentials.make_grant(hashed, found.share_id, share.now())
        share.set_cookie(
            response,
            credentials.GRANT_COOKIE,
            grant,
            max_age=int(credentials.GRANT_LIFETIME.total_seconds()),
        )
        return response
    _record(share, found, "password_failed")
    context["error"] = "That isn't the password."
    return share.render("public/password.html", context, status=403)


def _serve(share: ShareRequest, found: store.Share, item: SharedItem) -> Response:
    sub = share.subpath
    query = share.query
    if query.get("zip") == "1":
        response = item.zip_response(sub, name=item.entry(sub).name or item.name)
        _record(share, found, "zip")
        return response
    if query.get("raw") == "1":
        inline = query.get("inline") == "1"
        response = item.file_response(sub, inline=inline)
        if not inline:
            _record(share, found, "download")
        return response
    entry = item.entry(sub)
    _record(share, found, "view")
    common: dict[str, Any] = {
        "item_name": item.name,
        "expires_at": found.expires_at,
        "zip_href": _href(share, sub, zip="1"),
    }
    if entry.kind == "dir":
        listing = item.listing(sub)
        rows = [
            {
                "entry": e,
                "href": _href(share, e.path),
                "download": _href(share, e.path, raw="1") if e.kind == "file" else None,
            }
            for e in listing.entries
        ]
        crumbs = [{"name": c.name, "href": _href(share, c.path)} for c in listing.crumbs]
        return share.render(
            "public/folder.html",
            {**common, "listing": listing, "rows": rows, "crumbs": crumbs},
        )
    kind = preview_kind(entry)
    text, cut = ("", False)
    if kind == "text":
        try:
            text, cut = item.read_text(sub, max_bytes=TEXT_PREVIEW_BYTES)
        except NotFound:
            kind = None
    parent = sub.rsplit("/", 1)[0] if "/" in sub else ""
    return share.render(
        "public/file.html",
        {
            **common,
            "entry": entry,
            "preview": kind,
            "text": text,
            "cut": cut,
            "download": _href(share, sub, raw="1"),
            "inline": _href(share, sub, raw="1", inline="1"),
            "back": _href(share, parent) if sub else None,
        },
    )
