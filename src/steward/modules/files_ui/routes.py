# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The file manager's routes. Plain HTML forms: selections are checkboxes named `path`,
changes are POSTs (the core checks the CSRF token), and sensitive ones (delete, anything
in a locked folder) send you to the password page first.

The same routes serve two pages: `/ui/files` (the normal scope) and `/ui/locked` (the
Locked Folder: the locked scope, where every request needs a recent password check
before anything else runs). `make_router(scope)` builds each one."""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import Response

from steward.sdk import ActionFailed, ActionRefused, Entry, NotFound, Scope, WebDep

PAGE = "/ui/files"
LOCKED_PAGE = "/ui/locked"
TEXT_PREVIEW_BYTES = 256 * 1024
MAX_SELECTION = 1000
PER_PAGE = 100
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
    "move_out": "Moved {n} item(s) out of the Locked Folder.",
    "delete": "Moved {n} item(s) to the trash.",
    "undo": "Undone.",
    "lock": "Folder locked. It now shows only in the Locked Folder.",
    "permanent": "The lock is now permanent.",
    "unlock": "Folder unlocked. It shows in normal views again.",
}
"""Messages after a redirect (`?did=<key>&n=<count>`): fixed text, nothing reflected."""

PathQ = Annotated[str, Query(max_length=4096)]
Paths = Annotated[list[str], Query(alias="path", max_length=MAX_SELECTION)]
ScopeQ = Annotated[Scope, Query()]


def base_of(scope: Scope) -> str:
    return LOCKED_PAGE if scope == "locked" else PAGE


def folder_url(path: str, *, scope: Scope = "normal", **extra: Any) -> str:
    query = urlencode({"path": path, **extra}, safe=":/", quote_via=quote)
    return f"{base_of(scope)}?{query}"


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


def _gate(web: WebDep) -> None:
    """Every Locked Folder request: a recent password check first (checked without
    using up a one-shot re-auth, so browsing doesn't ask on every page)."""
    if not web.recently_authenticated():
        web.require_reauth()


def make_router(scope: Scope) -> APIRouter:
    locked = scope == "locked"
    base = base_of(scope)
    router = APIRouter(dependencies=[Depends(_gate)] if locked else [])

    def url(path: str, **extra: Any) -> str:
        return folder_url(path, scope=scope, **extra)

    def common(web: WebDep, **extra: Any) -> dict[str, Any]:
        # The shares module's dialog, if it's enabled; never offered for locked folders.
        shares = None if locked else web.page_url("shares.page")
        return {"base": base, "scope": scope, "kinds": KINDS, "shares": shares, **extra}

    # --- browsing -----------------------------------------------------------------------

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
        context = common(
            web,
            libraries=libraries,
            done_notice=_done(did, n),
            undo=",".join(str(i) for i in _undo_ids(undo)),
        )
        if kind:
            if kind not in dict(KINDS):
                raise NotFound(kind)
            found = web.files.by_kind(
                kind,
                library=library or None,
                scope=scope,
                limit=PER_PAGE,
                offset=(page - 1) * PER_PAGE,
            )
            context.update(
                kind=kind,
                kind_title=dict(KINDS)[kind],
                found=found,
                page=page,
                per_page=PER_PAGE,
                library=library,
            )
            return web.render("kind.html", context)
        if not path:
            if locked:
                context["folders"] = web.files.locked_folders()
                return web.render("locked.html", context)
            if len(libraries) == 1:
                return web.redirect(url(libraries[0].path))
            return web.render("libraries.html", context)
        listing = web.files.listing(path, scope=scope)
        context["listing"] = listing
        context["back"] = url(listing.path)
        return web.render("browse.html", context)

    @router.get("/view")
    def view(web: WebDep, path: PathQ) -> Response:
        entry = web.files.entry(path, scope=scope)
        if entry.kind == "dir":
            return web.redirect(url(path))
        text: str | None = None
        cut = False
        if _shows_as_text(entry):
            text, cut = web.files.read_text(path, scope=scope, max_bytes=TEXT_PREVIEW_BYTES)
        return web.render(
            "view.html",
            common(
                web,
                entry=entry,
                folder=parent_of(entry.path),
                preview=_preview_kind(entry),
                text=text,
                cut=cut,
            ),
        )

    @router.get("/raw")
    def raw(web: WebDep, path: PathQ, inline: bool = False) -> Response:
        return web.files.file_response(path, scope=scope, inline=inline)

    @router.get("/zip")
    def download_zip(
        web: WebDep, paths: Paths, name: Annotated[str, Query(max_length=200)] = ""
    ) -> Response:
        if not paths:
            raise NotFound("nothing selected")
        if not name:
            name = name_of(paths[0]) if len(paths) == 1 else name_of(parent_of(paths[0]))
            name = name or paths[0].partition(":")[0] or "files"
        return web.files.zip_response(paths, scope=scope, name=name)

    if locked:

        @router.get("/search")
        def search(
            web: WebDep,
            q: Annotated[str, Query(max_length=500)] = "",
            page: Annotated[int, Query(ge=1, le=10_000)] = 1,
        ) -> Response:
            context = common(web, q=q, page=page, per_page=PER_PAGE, found=None)
            if q.strip():
                try:
                    context["found"] = web.files.search(
                        q, scope="locked", limit=PER_PAGE, offset=(page - 1) * PER_PAGE
                    )
                except ValueError as exc:
                    context["error"] = str(exc)
            return web.render("search.html", context)

    # --- changes ------------------------------------------------------------------------

    @router.post("/mkdir")
    def mkdir(
        web: WebDep,
        folder: Annotated[str, Form(max_length=4096)],
        name: Annotated[str, Form(min_length=1, max_length=255)],
    ) -> Response:
        listing = web.files.listing(folder, scope=scope)
        if "/" in name or name.strip() in ("", ".", ".."):
            return browse_error(web, listing.path, "A folder name can't contain '/' or be empty.")
        try:
            done = web.actions.run("file.mkdir", {"path": join(listing.path, name.strip())})
        except (ActionRefused, ActionFailed) as exc:
            return browse_error(web, listing.path, str(exc))
        return web.redirect(url(listing.path, did="mkdir", undo=done.entry_id))

    @router.get("/rename")
    def rename_form(web: WebDep, path: PathQ) -> Response:
        entry = web.files.entry(path, scope=scope)
        return web.render("rename.html", common(web, entry=entry, folder=parent_of(entry.path)))

    @router.post("/rename")
    def rename(
        web: WebDep,
        path: Annotated[str, Form(max_length=4096)],
        new_name: Annotated[str, Form(min_length=1, max_length=255)],
    ) -> Response:
        entry = web.files.entry(path, scope=scope)
        folder = parent_of(entry.path)
        try:
            done = web.actions.run(
                "file.rename", {"path": entry.path, "new_name": new_name.strip()}
            )
        except (ActionRefused, ActionFailed) as exc:
            context = common(web, entry=entry, folder=folder, error=str(exc))
            return web.render("rename.html", context, status=409)
        return web.redirect(url(folder, did="rename", undo=done.entry_id))

    def dest_scope(to_scope: Scope) -> Scope:
        # Only the Locked Folder moves things elsewhere (out of it, after a warning).
        return to_scope if locked else "normal"

    def move_page(
        web: WebDep, entries: list[Entry], target: str, to_scope: Scope, **extra: Any
    ) -> Response:
        listing = web.files.listing(target, scope=to_scope)
        return web.render(
            "move.html",
            common(
                web,
                entries=entries,
                listing=listing,
                libraries=web.files.libraries(),
                selected=[e.path for e in entries],
                to_scope=to_scope,
                moving_out=locked and to_scope == "normal",
                moving_into_itself=any(
                    listing.path == e.path or listing.path.startswith(e.path + "/") for e in entries
                ),
                **extra,
            ),
            status=409 if "error" in extra else 200,
        )

    @router.get("/move")
    def move_form(
        web: WebDep, paths: Paths, to: PathQ = "", to_scope: ScopeQ = "locked"
    ) -> Response:
        entries = _selection(web, paths, scope)
        target_scope = dest_scope(to_scope)
        target = to
        if not target:
            if target_scope == scope:
                target = parent_of(entries[0].path)
            else:
                libraries = web.files.libraries()
                if not libraries:
                    raise NotFound("no libraries")
                target = libraries[0].path
        return move_page(web, entries, target, target_scope)

    @router.post("/move")
    def move(
        web: WebDep,
        paths: Annotated[list[str], Form(alias="path", max_length=MAX_SELECTION)],
        to: Annotated[str, Form(max_length=4096)],
        to_scope: Annotated[Scope, Form()] = "locked",
        confirm_out: Annotated[bool, Form()] = False,
    ) -> Response:
        entries = _selection(web, paths, scope)
        target_scope = dest_scope(to_scope)
        moving_out = locked and target_scope == "normal"
        if moving_out and not confirm_out:
            return move_page(
                web,
                entries,
                to,
                target_scope,
                error="Tick the box to confirm that these items should leave the Locked Folder.",
            )
        destination = web.files.listing(to, scope=target_scope)
        steps = [
            ("file.move", {"source": e.path, "destination": join(destination.path, e.name)})
            for e in entries
        ]
        outcomes = web.actions.run_all(steps)
        if moving_out:
            return after(web, "move_out", destination.path, outcomes, to_scope="normal")
        return after(web, "move", parent_of(entries[0].path), outcomes)

    @router.get("/delete")
    def delete_form(web: WebDep, paths: Paths) -> Response:
        entries = _selection(web, paths, scope)
        return web.render(
            "delete.html",
            common(
                web,
                entries=entries,
                folder=parent_of(entries[0].path),
                needs_password=not web.recently_authenticated(),
            ),
        )

    @router.post("/delete")
    def delete(
        web: WebDep,
        paths: Annotated[list[str], Form(alias="path", max_length=MAX_SELECTION)],
    ) -> Response:
        entries = _selection(web, paths, scope)
        outcomes = web.actions.run_all([("file.delete", {"path": e.path}) for e in entries])
        return after(web, "delete", parent_of(entries[0].path), outcomes)

    # --- locks ----------------------------------------------------------------------------

    @router.get("/lock")
    def lock_form(web: WebDep, path: PathQ) -> Response:
        listing = web.files.listing(path, scope=scope)
        return web.render("lock.html", common(web, listing=listing, folder=parent_of(path)))

    @router.post("/lock")
    def lock(
        web: WebDep,
        path: Annotated[str, Form(max_length=4096)],
        permanent: Annotated[bool, Form()] = False,
    ) -> Response:
        listing = web.files.listing(path, scope=scope)
        if locked and not permanent:
            raise NotFound("already locked")  # the Locked Folder only makes locks permanent
        try:
            done = web.actions.run("folder.lock", {"path": listing.path, "permanent": permanent})
        except (ActionRefused, ActionFailed) as exc:
            context = common(web, listing=listing, folder=parent_of(path), error=str(exc))
            return web.render("lock.html", context, status=409)
        if locked:
            return web.redirect(url(listing.path, did="permanent"))
        extra = {"undo": done.entry_id} if done.undoable else {}
        return web.redirect(url(parent_of(listing.path), did="lock", **extra))

    if locked:

        @router.get("/unlock")
        def unlock_form(web: WebDep, path: PathQ) -> Response:
            preview = web.files.unlock_preview(path)
            return web.render("unlock.html", common(web, preview=preview, needs_password=True))

        @router.post("/unlock")
        def unlock(web: WebDep, path: Annotated[str, Form(max_length=4096)]) -> Response:
            preview = web.files.unlock_preview(path)
            try:
                done = web.actions.run("folder.unlock", {"path": preview.path})
            except (ActionRefused, ActionFailed) as exc:
                context = common(web, preview=preview, error=str(exc))
                return web.render("unlock.html", context, status=409)
            return web.redirect(folder_url(preview.path, did="unlock", undo=done.entry_id))

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
            context = common(web, errors=errors, back=back or base)
            return web.render("errors.html", context, status=409)
        return web.redirect(url(back, did="undo") if back else base)

    # --- helpers --------------------------------------------------------------------------

    def browse_error(web: WebDep, folder: str, message: str) -> Response:
        listing = web.files.listing(folder, scope=scope)
        context = common(
            web,
            listing=listing,
            libraries=web.files.libraries(),
            error=message,
            back=url(listing.path),
        )
        return web.render("browse.html", context, status=409)

    def after(
        web: WebDep, did: str, folder: str, outcomes: list[Any], *, to_scope: Scope | None = None
    ) -> Response:
        done = [o.done.entry_id for o in outcomes if o.done is not None]
        errors = [f"{name_of(_step_path(o.params))}: {o.error}" for o in outcomes if o.error]
        target_scope = to_scope or scope
        if errors:
            context = common(
                web,
                errors=errors,
                back=folder_url(folder, scope=target_scope),
                done=len(done),
                undo=",".join(map(str, done)),
                folder=folder,
                undo_base=base_of(target_scope),
            )
            return web.render("errors.html", context, status=409)
        return web.redirect(
            folder_url(
                folder, scope=target_scope, did=did, n=len(done), undo=",".join(map(str, done))
            )
        )

    return router


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


def _selection(web: WebDep, paths: list[str], scope: Scope) -> list[Entry]:
    unique = list(dict.fromkeys(p for p in paths if p))
    if not unique:
        raise NotFound("nothing selected")
    return [web.files.entry(p, scope=scope) for p in unique]


def _step_path(params: Any) -> str:
    return str(params.get("source") or params.get("path") or "")


router = make_router("normal")
locked_router = make_router("locked")
