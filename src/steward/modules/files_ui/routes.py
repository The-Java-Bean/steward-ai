# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The file manager's routes. Plain HTML forms: selections are checkboxes named `path`,
changes are POSTs (the core checks the CSRF token), and sensitive ones (delete, anything
in a locked folder) send you to the password page first."""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, Query
from fastapi.responses import Response

from steward.sdk import ActionFailed, ActionRefused, Entry, NotFound, WebDep

PAGE = "/ui/files"
TEXT_PREVIEW_BYTES = 256 * 1024
MAX_SELECTION = 1000
KINDS = (
    ("image", "Images"),
    ("video", "Videos"),
    ("audio", "Audio"),
    ("document", "Documents"),
    ("text", "Text"),
    ("archive", "Archives"),
    ("other", "Other"),
)
DONE = {
    "mkdir": "Folder created.",
    "rename": "Renamed.",
    "move": "Moved {n} item(s).",
    "delete": "Moved {n} item(s) to the trash.",
    "undo": "Undone.",
}
"""Messages after a redirect (`?did=<key>&n=<count>`): fixed text, nothing reflected."""

PathQ = Annotated[str, Query(max_length=4096)]
Paths = Annotated[list[str], Query(alias="path", max_length=MAX_SELECTION)]

router = APIRouter()


def folder_url(path: str, **extra: Any) -> str:
    query = urlencode({"path": path, **extra}, safe=":/", quote_via=quote)
    return f"{PAGE}?{query}"


def parent_of(path: str) -> str:
    library, _, relpath = path.partition(":")
    return f"{library}:{relpath.rsplit('/', 1)[0] if '/' in relpath else ''}"


def name_of(path: str) -> str:
    relpath = path.partition(":")[2]
    return relpath.rsplit("/", 1)[-1]


def join(folder: str, name: str) -> str:
    return f"{folder}{name}" if folder.endswith(":") else f"{folder}/{name}"


def _done(did: str, n: int) -> str | None:
    text = DONE.get(did)
    return text.format(n=n) if text else None


def _undo_ids(value: str) -> list[int]:
    ids = [int(v) for v in value.split(",") if v.isdigit()]
    return ids[:MAX_SELECTION]


# --- browsing ---------------------------------------------------------------------------


@router.get("")
def browse(
    web: WebDep,
    path: PathQ = "",
    kind: Annotated[str, Query(max_length=16)] = "",
    library: Annotated[str, Query(max_length=64)] = "",
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    did: Annotated[str, Query(max_length=16)] = "",
    n: Annotated[int, Query(ge=0, le=MAX_SELECTION)] = 1,
    undo: Annotated[str, Query(max_length=10_000)] = "",
) -> Response:
    libraries = web.files.libraries()
    context: dict[str, Any] = {
        "kinds": KINDS,
        "libraries": libraries,
        "notice": _done(did, n),
        "undo": ",".join(str(i) for i in _undo_ids(undo)),
    }
    if kind:
        if kind not in dict(KINDS):
            raise NotFound(kind)
        per_page = 100
        found = web.files.by_kind(
            kind, library=library or None, limit=per_page, offset=(page - 1) * per_page
        )
        context.update(
            kind=kind,
            kind_title=dict(KINDS)[kind],
            found=found,
            page=page,
            per_page=per_page,
            library=library,
        )
        return web.render("kind.html", context)
    if not path:
        if len(libraries) == 1:
            return web.redirect(folder_url(libraries[0].path))
        return web.render("libraries.html", context)
    listing = web.files.listing(path)
    context["listing"] = listing
    context["back"] = folder_url(listing.path)
    return web.render("browse.html", context)


@router.get("/view")
def view(web: WebDep, path: PathQ) -> Response:
    entry = web.files.entry(path)
    if entry.kind == "dir":
        return web.redirect(folder_url(path))
    text: str | None = None
    cut = False
    if _shows_as_text(entry):
        text, cut = web.files.read_text(path, max_bytes=TEXT_PREVIEW_BYTES)
    return web.render(
        "view.html",
        {
            "entry": entry,
            "folder": parent_of(entry.path),
            "preview": _preview_kind(entry),
            "text": text,
            "cut": cut,
        },
    )


def _preview_kind(entry: Entry) -> str | None:
    mime = entry.mime or ""
    if mime in ("image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime == "application/pdf":
        return "pdf"
    if _shows_as_text(entry):
        return "text"
    return None


def _shows_as_text(entry: Entry) -> bool:
    return entry.file_kind == "text" or (entry.mime or "").startswith("text/")


@router.get("/raw")
def raw(web: WebDep, path: PathQ, inline: bool = False) -> Response:
    return web.files.file_response(path, inline=inline)


@router.get("/zip")
def download_zip(
    web: WebDep, paths: Paths, name: Annotated[str, Query(max_length=200)] = ""
) -> Response:
    if not paths:
        raise NotFound("nothing selected")
    if not name:
        name = name_of(paths[0]) if len(paths) == 1 else name_of(parent_of(paths[0])) or "files"
        name = name or paths[0].partition(":")[0]
    return web.files.zip_response(paths, name=name)


# --- changes ---------------------------------------------------------------------------


@router.post("/mkdir")
def mkdir(
    web: WebDep,
    folder: Annotated[str, Form(max_length=4096)],
    name: Annotated[str, Form(min_length=1, max_length=255)],
) -> Response:
    listing = web.files.listing(folder)
    if "/" in name or name.strip() in ("", ".", ".."):
        return _browse_error(web, listing.path, "A folder name can't contain '/' or be empty.")
    try:
        done = web.actions.run("file.mkdir", {"path": join(listing.path, name.strip())})
    except (ActionRefused, ActionFailed) as exc:
        return _browse_error(web, listing.path, str(exc))
    return web.redirect(folder_url(listing.path, did="mkdir", undo=done.entry_id))


@router.get("/rename")
def rename_form(web: WebDep, path: PathQ) -> Response:
    entry = web.files.entry(path)
    return web.render("rename.html", {"entry": entry, "folder": parent_of(entry.path)})


@router.post("/rename")
def rename(
    web: WebDep,
    path: Annotated[str, Form(max_length=4096)],
    new_name: Annotated[str, Form(min_length=1, max_length=255)],
) -> Response:
    entry = web.files.entry(path)
    folder = parent_of(entry.path)
    try:
        done = web.actions.run("file.rename", {"path": entry.path, "new_name": new_name.strip()})
    except (ActionRefused, ActionFailed) as exc:
        context = {"entry": entry, "folder": folder, "error": str(exc)}
        return web.render("rename.html", context, status=409)
    return web.redirect(folder_url(folder, did="rename", undo=done.entry_id))


@router.get("/move")
def move_form(web: WebDep, paths: Paths, to: PathQ = "") -> Response:
    entries = _selection(web, paths)
    target = to or parent_of(entries[0].path)
    listing = web.files.listing(target)
    return web.render(
        "move.html",
        {
            "entries": entries,
            "listing": listing,
            "libraries": web.files.libraries(),
            "selected": [e.path for e in entries],
            "moving_into_itself": any(
                listing.path == e.path or listing.path.startswith(e.path + "/") for e in entries
            ),
        },
    )


@router.post("/move")
def move(
    web: WebDep,
    paths: Annotated[list[str], Form(alias="path", max_length=MAX_SELECTION)],
    to: Annotated[str, Form(max_length=4096)],
) -> Response:
    entries = _selection(web, paths)
    destination = web.files.listing(to)
    steps = [
        ("file.move", {"source": e.path, "destination": join(destination.path, e.name)})
        for e in entries
    ]
    outcomes = web.actions.run_all(steps)
    return _after(web, "move", parent_of(entries[0].path), outcomes)


@router.get("/delete")
def delete_form(web: WebDep, paths: Paths) -> Response:
    entries = _selection(web, paths)
    return web.render(
        "delete.html",
        {
            "entries": entries,
            "folder": parent_of(entries[0].path),
            "needs_password": not web.recently_authenticated(),
        },
    )


@router.post("/delete")
def delete(
    web: WebDep,
    paths: Annotated[list[str], Form(alias="path", max_length=MAX_SELECTION)],
) -> Response:
    entries = _selection(web, paths)
    outcomes = web.actions.run_all([("file.delete", {"path": e.path}) for e in entries])
    return _after(web, "delete", parent_of(entries[0].path), outcomes)


@router.post("/undo")
def undo(
    web: WebDep,
    entries: Annotated[str, Form(max_length=10_000)],
    back: Annotated[str, Form(max_length=4096)] = "",
) -> Response:
    errors: list[str] = []
    for entry_id in reversed(_undo_ids(entries)):
        try:
            web.actions.undo(entry_id)
        except (ActionRefused, ActionFailed) as exc:
            errors.append(str(exc))
    if errors:
        return web.render("errors.html", {"errors": errors, "back": back or PAGE}, status=409)
    return web.redirect(folder_url(back, did="undo") if back else PAGE)


# --- helpers ---------------------------------------------------------------------------


def _selection(web: WebDep, paths: list[str]) -> list[Entry]:
    unique = list(dict.fromkeys(p for p in paths if p))
    if not unique:
        raise NotFound("nothing selected")
    return [web.files.entry(p) for p in unique]


def _browse_error(web: WebDep, folder: str, message: str) -> Response:
    listing = web.files.listing(folder)
    context = {
        "listing": listing,
        "kinds": KINDS,
        "libraries": web.files.libraries(),
        "error": message,
        "back": folder_url(listing.path),
    }
    return web.render("browse.html", context, status=409)


def _after(web: WebDep, did: str, folder: str, outcomes: list[Any]) -> Response:
    done = [o.done.entry_id for o in outcomes if o.done is not None]
    errors = [f"{name_of(_step_path(o.params))}: {o.error}" for o in outcomes if o.error]
    if errors:
        context = {
            "errors": errors,
            "back": folder_url(folder),
            "done": len(done),
            "undo": ",".join(map(str, done)),
            "folder": folder,
        }
        return web.render("errors.html", context, status=409)
    return web.redirect(folder_url(folder, did=did, n=len(done), undo=",".join(map(str, done))))


def _step_path(params: Any) -> str:
    return str(params.get("source") or params.get("path") or "")
