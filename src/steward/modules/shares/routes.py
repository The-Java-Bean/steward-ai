# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`/ui/shares`: every share, active and past; the share dialog; changing and revoking.

Items that are now hidden in normal views (moved into a locked folder, say) are listed
without their name, and the text filter only looks at names that are shown, so this
page never says what is inside a locked folder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Form, Path, Query
from fastapi.responses import Response

from steward.modules.shares import credentials, store
from steward.modules.shares.actions import SHARE_ID
from steward.sdk import ActionFailed, ActionRefused, NotFound, PathPrivacy, Web, WebDep, utcnow

PAGE = "/ui/shares"
HIDDEN_NAME = "An item in a locked or hidden folder"
PRESETS = ("1", "7", "30")
DEFAULT_DAYS = "7"
MAX_DAYS = 36_500
AGES = (("", "Any time"), ("7", "Last 7 days"), ("30", "Last 30 days"), ("365", "Last year"))
DONE = {
    "revoked": "The link is turned off. It is listed under Past.",
    "updated": "The share was changed.",
    "undone": "Undone.",
}
"""Messages after a redirect (`?did=<key>`): fixed text, nothing reflected."""

ShareId = Annotated[str, Path(pattern=SHARE_ID)]
router = APIRouter()


@dataclass(frozen=True, slots=True)
class Row:
    share: store.Share
    name: str | None
    """The item's path, or None when it's hidden in normal views."""
    status: str
    ended: datetime | None
    link: str | None
    item_href: str | None
    problem: str | None
    """Why the link doesn't work right now even though the share is active."""
    by: str


def _rows(web: Web, shares: list[store.Share], now: datetime) -> list[Row]:
    privacy = web.files.privacy([s.path for s in shares])
    return [_row(web, s, privacy[s.path], now) for s in shares]


def _row(web: Web, share: store.Share, privacy: PathPrivacy, now: datetime) -> Row:
    status = share.status(now)
    hidden = privacy.hidden
    return Row(
        share=share,
        name=None if hidden else share.path,
        status=status,
        ended=share.ended(now),
        link=web.absolute_url(f"/s/{share.token}") if status == "active" else None,
        item_href=None if hidden else web.link_to(share.path, share.kind),
        problem=(
            None
            if status != "active" or privacy.shareable
            else "The item is in a locked or hidden folder now."
            if hidden
            else privacy.reason
        ),
        by="you" if share.created_by == web.viewer.actor else share.created_by,
    )


def _get(web: Web, share_id: str) -> store.Share:
    with web.state.read() as conn:
        found = store.get(conn, share_id)
    if found is None:
        raise NotFound(share_id)
    return found


def _expiry(choice: str, days: int | None, now: datetime) -> datetime | None:
    """`never` → None; a preset or `custom` (with `days`) → that many days from now."""
    if choice == "never":
        return None
    if choice in PRESETS:
        return now + timedelta(days=int(choice))
    if choice == "custom" and days is not None and 1 <= days <= MAX_DAYS:
        return now + timedelta(days=days)
    raise ActionRefused(f"choose how long the link works (1 to {MAX_DAYS} days)")


def _now(web: Web) -> datetime:
    return utcnow()


def _days(value: str) -> int | None:
    value = value.strip()
    return int(value) if value.isdigit() and len(value) <= 6 else None


# --- the list ------------------------------------------------------------------------------


@router.get("")
def shares(
    web: WebDep,
    status: Annotated[str, Query(pattern="^(active|past)$")] = "active",
    q: Annotated[str, Query(max_length=200)] = "",
    age: Annotated[str, Query(pattern="^(|7|30|365)$")] = "",
    did: Annotated[str, Query(max_length=16)] = "",
) -> Response:
    now = _now(web)
    since = now - timedelta(days=int(age)) if age else None
    with web.state.read() as conn:
        found = store.listing(conn, now=now, past=status == "past", since=since)
    rows = _rows(web, found, now)
    if q:
        needle = q.casefold()
        rows = [r for r in rows if r.name is not None and needle in r.name.casefold()]
    return web.render(
        "list.html",
        {
            "rows": rows,
            "status": status,
            "q": q,
            "age": age,
            "ages": AGES,
            "notice": DONE.get(did),
        },
    )


# --- the share dialog ----------------------------------------------------------------------


def _dialog(
    web: Web,
    path: str,
    *,
    error: str | None = None,
    status: int = 200,
    form: dict[str, Any] | None = None,
) -> Response:
    entry = web.files.entry(path)  # NotFound for anything hidden in normal views
    privacy = web.files.privacy([path])[path]
    values = {
        "expiry": DEFAULT_DAYS,
        "days": "",
        "password_on": True,
        "password": credentials.suggest_password(),
        "snapshot": False,
        **(form or {}),
    }
    return web.render(
        "new.html",
        {
            "entry": entry,
            "privacy": privacy,
            "is_root": path.endswith(":"),
            "values": values,
            "presets": PRESETS,
            "error": error,
            "min_password": credentials.MIN_PASSWORD,
        },
        status=status,
    )


@router.get("/new")
def new_share(web: WebDep, path: Annotated[str, Query(max_length=4096)]) -> Response:
    return _dialog(web, path)


@router.post("/new")
def create_share(
    web: WebDep,
    path: Annotated[str, Form(max_length=4096)],
    expiry: Annotated[str, Form(max_length=8)] = DEFAULT_DAYS,
    days: Annotated[str, Form(max_length=16)] = "",
    password_on: Annotated[str, Form(max_length=1)] = "",
    password: Annotated[str, Form(max_length=credentials.MAX_PASSWORD)] = "",
    snapshot: Annotated[str, Form(max_length=1)] = "",
) -> Response:
    web.files.entry(path)  # 404 before anything else for hidden paths
    form = {
        "expiry": expiry,
        "days": days,
        "password_on": bool(password_on),
        "password": password,
        "snapshot": bool(snapshot),
    }
    share_id = credentials.new_share_id()
    try:
        expires_at = _expiry(expiry, _days(days), _now(web))
        if password_on and len(password) < credentials.MIN_PASSWORD:
            raise ActionRefused(
                f"the password needs at least {credentials.MIN_PASSWORD} characters"
            )
        done = web.actions.run(
            "shares.create",
            {
                "path": path,
                "expires_at": expires_at.isoformat() if expires_at else None,
                "password": password if password_on else None,
                "snapshot": bool(snapshot),
                "share_id": share_id,
            },
        )
    except (ActionRefused, ActionFailed) as exc:
        return _dialog(web, path, error=str(exc), status=409, form=form)
    created = _get(web, share_id)
    return web.render(
        "created.html",
        {
            "row": _row(web, created, web.files.privacy([path])[path], _now(web)),
            "password": password if password_on else None,
            "entry_id": done.entry_id,
        },
    )


# --- one share ----------------------------------------------------------------------------


@router.get("/{share_id}")
def share_page(
    web: WebDep, share_id: ShareId, did: Annotated[str, Query(max_length=16)] = ""
) -> Response:
    found = _get(web, share_id)
    now = _now(web)
    with web.state.read() as conn:
        log = store.accesses(conn, share_id)
    return web.render(
        "detail.html",
        {
            "row": _row(web, found, web.files.privacy([found.path])[found.path], now),
            "log": log,
            "notice": DONE.get(did),
        },
    )


def _edit(web: Web, found: store.Share, *, error: str | None = None, status: int = 200) -> Response:
    now = _now(web)
    row = _row(web, found, web.files.privacy([found.path])[found.path], now)
    return web.render(
        "edit.html",
        {"row": row, "presets": PRESETS, "error": error, "min_password": credentials.MIN_PASSWORD},
        status=status,
    )


@router.get("/{share_id}/edit")
def edit_page(web: WebDep, share_id: ShareId) -> Response:
    found = _get(web, share_id)
    if not found.active(_now(web)):
        return web.redirect(f"{PAGE}/{share_id}")
    return _edit(web, found)


@router.post("/{share_id}/edit")
def edit_share(
    web: WebDep,
    share_id: ShareId,
    expiry: Annotated[str, Form(max_length=8)] = "keep",
    days: Annotated[str, Form(max_length=16)] = "",
    password_change: Annotated[str, Form(pattern="^(keep|set|remove)$")] = "keep",
    new_password: Annotated[str, Form(max_length=credentials.MAX_PASSWORD)] = "",
) -> Response:
    found = _get(web, share_id)
    try:
        params: dict[str, Any] = {"share_id": share_id, "password_change": password_change}
        if expiry == "keep":
            params["expiry"] = "keep"
        else:
            expires_at = _expiry(expiry, _days(days), _now(web))
            params["expiry"] = "never" if expires_at is None else "set"
            params["expires_at"] = expires_at.isoformat() if expires_at else None
        if password_change == "set":
            if len(new_password) < credentials.MIN_PASSWORD:
                raise ActionRefused(
                    f"the password needs at least {credentials.MIN_PASSWORD} characters"
                )
            params["new_password"] = new_password
        web.actions.run("shares.update", params)
    except (ActionRefused, ActionFailed) as exc:
        return _edit(web, found, error=str(exc), status=409)
    return web.redirect(f"{PAGE}/{share_id}?did=updated")


@router.post("/{share_id}/revoke")
def revoke_share(web: WebDep, share_id: ShareId) -> Response:
    _get(web, share_id)
    try:
        web.actions.run("shares.revoke", {"share_id": share_id})
    except (ActionRefused, ActionFailed) as exc:
        return web.render("errors.html", {"error": str(exc)}, status=409)
    return web.redirect(f"{PAGE}/{share_id}?did=revoked")


@router.post("/undo")
def undo(web: WebDep, entry: Annotated[int, Form(ge=1)]) -> Response:
    try:
        web.actions.undo(entry)
    except (ActionRefused, ActionFailed) as exc:
        return web.render("errors.html", {"error": str(exc)}, status=409)
    return web.redirect(f"{PAGE}?did=undone")


def new_link(path: str) -> str:
    """The share dialog for `path` (for other pages that link to it)."""
    return f"{PAGE}/new?path={quote(path, safe=':/')}"
