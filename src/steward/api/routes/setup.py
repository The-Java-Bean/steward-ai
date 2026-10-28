# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The first-run setup wizard (plan §6.4, docs/adr/0013).

`/setup` asks for the setup token from the log; `/setup/owner` (token cookie needed,
only while no owner exists) creates the owner and logs them in. The remaining steps
need that session. Once setup is finished they redirect to the home page.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from steward.api.security import (
    SESSION_COOKIE,
    SETUP_COOKIE,
    access,
    api_state,
    require_principal,
)
from steward.api.templating import clear_cookie, redirect, render, session_max_age, set_cookie
from steward.core import setup
from steward.core.auth import AccountError, WeakPasswordError
from steward.core.folders import FolderError, FolderRef, PathError, Role, load_policy
from steward.core.runtime import Runtime

router = APIRouter()
SETUP_COOKIE_SECONDS = 3600
_PAGES = {
    "libraries": "/setup/libraries",
    "types": "/setup/types",
    "private": "/setup/private",
    "roles": "/setup/roles",
    "done": "/setup/done",
}


def _runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


def progress_of(request: Request) -> setup.Progress:
    with _runtime(request).dbs.state.read() as conn:
        return setup.progress(conn, has_owner=api_state(request).has_owner())


def next_page(request: Request) -> str:
    """Where an unfinished setup continues (used by the home page and after login)."""
    progress = progress_of(request)
    return "/ui/" if progress.finished else _PAGES.get(progress.step, "/setup/libraries")


def _open_step(request: Request) -> Response | None:
    """Wizard steps are closed once setup is finished."""
    if progress_of(request).finished:
        return redirect("/ui/")
    return None


def _actor(request: Request) -> str:
    return require_principal(request).actor


# --- token and owner ------------------------------------------------------------------


@router.get("/setup")
@access("public")
def token_page(request: Request) -> Response:
    state = api_state(request)
    if state.has_owner():
        return redirect(next_page(request) if request.state.principal else "/login")
    if state.setup_token is not None and state.setup_token.check(request.cookies.get(SETUP_COOKIE)):
        return redirect("/setup/owner")
    return render(request, "setup/token.html")


@router.post("/setup")
@access("public")
def check_token(request: Request, token: Annotated[str, Form(max_length=256)]) -> Response:
    state = api_state(request)
    if state.has_owner():
        return redirect("/login")
    if state.setup_token is None or not state.setup_token.check(token):
        return render(
            request,
            "setup/token.html",
            {"error": "That isn't the setup token. Copy it from the steward-ai log."},
            status=403,
        )
    response = redirect("/setup/owner")
    set_cookie(request, response, SETUP_COOKIE, token.strip(), max_age=SETUP_COOKIE_SECONDS)
    return response


@router.get("/setup/owner")
@access("setup")
def owner_page(request: Request) -> Response:
    return render(request, "setup/owner.html", {"step": "owner"})


@router.post("/setup/owner")
@access("setup")
def create_owner(
    request: Request,
    username: Annotated[str, Form(max_length=256)],
    password: Annotated[str, Form(max_length=4096)],
    confirm: Annotated[str, Form(max_length=4096)],
) -> Response:
    state = api_state(request)
    context = {"step": "owner", "username": username}
    if password != confirm:
        return render(
            request,
            "setup/owner.html",
            {**context, "error": "The two passwords don't match."},
            status=400,
        )
    try:
        state.auth.create_owner(username, password, actor="setup")
    except (WeakPasswordError, AccountError) as exc:
        return render(request, "setup/owner.html", {**context, "error": str(exc)}, status=400)
    session = state.auth.login(
        username, password, ip=request.state.ip, user_agent=request.headers.get("user-agent")
    )
    with _runtime(request).dbs.state.write() as conn:
        setup.advance(conn, "libraries", actor=session.principal.actor)
    response = redirect("/setup/libraries")
    set_cookie(request, response, SESSION_COOKIE, session.secret, max_age=session_max_age(request))
    clear_cookie(request, response, SETUP_COOKIE)
    return response


# --- libraries ------------------------------------------------------------------------


def _libraries_page(
    request: Request, *, error: str | None = None, warnings: list[str] | None = None
) -> Response:
    runtime = _runtime(request)
    with runtime.dbs.state.read() as conn:
        policy = load_policy(conn)
    config = runtime.config
    return render(
        request,
        "setup/libraries.html",
        {
            "step": "libraries",
            "libraries": sorted(policy.libraries.values(), key=lambda lib: lib.position),
            "candidates": setup.library_candidates(config, policy),
            "can_import": bool(
                config.libraries.libraries
                or config.folders.mappings
                or config.folders.types
                or config.folders.roles
            ),
            "library_dirs": [str(p) for p in config.steward.setup.library_dirs],
            "error": error,
            "warnings": warnings or [],
        },
        status=400 if error else 200,
    )


@router.get("/setup/libraries")
def libraries_page(request: Request) -> Response:
    return _open_step(request) or _libraries_page(request)


@router.post("/setup/libraries")
def libraries(
    request: Request,
    action: Annotated[str, Form(max_length=20)],
    library_id: Annotated[str, Form(max_length=64)] = "",
    path: Annotated[str, Form(max_length=4096)] = "",
    name: Annotated[str, Form(max_length=200)] = "",
) -> Response:
    if (closed := _open_step(request)) is not None:
        return closed
    runtime = _runtime(request)
    actor = _actor(request)
    if action == "next":
        with runtime.dbs.state.write() as conn:
            if not load_policy(conn).libraries:
                return _libraries_page(request, error="Add at least one library first.")
            setup.advance(conn, "types", actor=actor)
        return redirect("/setup/types")
    try:
        with runtime.dbs.state.write() as conn:
            if action == "import":
                setup.import_config_files(conn, runtime.config, actor=actor)
                warnings: list[str] = []
            elif action == "add":
                warnings = setup.add_library(
                    conn, library_id.strip(), path.strip(), name=name.strip() or None, actor=actor
                )
            else:
                return _libraries_page(request, error="Unknown action.")
    except (setup.SetupError, FolderError, PathError) as exc:
        return _libraries_page(request, error=str(exc))
    return _libraries_page(request, warnings=warnings)


# --- folder types ---------------------------------------------------------------------


def _top_folders(request: Request) -> list[tuple[str, list[setup.TopFolder]]]:
    with _runtime(request).dbs.state.read() as conn:
        policy = load_policy(conn)
    return [
        (lib.library_id, setup.top_level_folders(policy, lib.library_id))
        for lib in sorted(policy.libraries.values(), key=lambda lib: lib.position)
    ]


def _type_names(request: Request) -> list[tuple[str, str]]:
    with _runtime(request).dbs.state.read() as conn:
        policy = load_policy(conn)
    return sorted((name, t.description) for name, t in policy.types.items())


@router.get("/setup/types")
def types_page(request: Request) -> Response:
    if (closed := _open_step(request)) is not None:
        return closed
    return render(
        request,
        "setup/types.html",
        {"step": "types", "groups": _top_folders(request), "types": _type_names(request)},
    )


@router.post("/setup/types")
async def apply_types(request: Request) -> Response:
    form = await request.form()
    choices: dict[FolderRef, str] = {}
    for key, value in form.multi_items():
        if not key.startswith("type:") or not isinstance(value, str) or not value:
            continue
        try:
            choices[FolderRef.parse(key.removeprefix("type:"))] = value
        except PathError:
            continue
    return await run_in_threadpool(_apply_types, request, choices)


def _apply_types(request: Request, choices: dict[FolderRef, str]) -> Response:
    if (closed := _open_step(request)) is not None:
        return closed
    try:
        with _runtime(request).dbs.state.write() as conn:
            setup.apply_types(conn, choices, actor=_actor(request))
            setup.advance(conn, "private", actor=_actor(request))
    except (setup.SetupError, FolderError, PathError) as exc:
        return render(
            request,
            "setup/types.html",
            {
                "step": "types",
                "groups": _top_folders(request),
                "types": _type_names(request),
                "error": str(exc),
            },
            status=400,
        )
    return redirect("/setup/private")


# --- private folder -------------------------------------------------------------------


def _private_page(request: Request, *, error: str | None = None) -> Response:
    with _runtime(request).dbs.state.read() as conn:
        policy = load_policy(conn)
    return render(
        request,
        "setup/private.html",
        {
            "step": "private",
            "groups": _top_folders(request),
            "libraries": sorted(policy.libraries.values(), key=lambda lib: lib.position),
            "error": error,
        },
        status=400 if error else 200,
    )


@router.get("/setup/private")
def private_page(request: Request) -> Response:
    return _open_step(request) or _private_page(request)


@router.post("/setup/private")
def choose_private(
    request: Request,
    action: Annotated[str, Form(max_length=20)],
    folder: Annotated[str, Form(max_length=4096)] = "",
    library_id: Annotated[str, Form(max_length=64)] = "",
    new_name: Annotated[str, Form(max_length=4096)] = "",
    permanent: Annotated[bool, Form()] = False,
) -> Response:
    if (closed := _open_step(request)) is not None:
        return closed
    actor = _actor(request)
    try:
        if action == "create":
            if not new_name.strip():
                raise setup.SetupError("Type a name for the new folder.")
            created = setup.parse_new_folder(library_id, new_name)
            setup.create_private_folder(_runtime(request).executor, created, actor=actor)
        with _runtime(request).dbs.state.write() as conn:
            if action == "skip":
                setup.skip_private(conn, actor=actor)
            elif action == "existing":
                if not folder:
                    raise setup.SetupError("Choose a folder.")
                ref = FolderRef.parse(folder)
                setup.choose_private(conn, ref, permanent=permanent, actor=actor)
            elif action == "create":
                setup.choose_private(conn, created, permanent=permanent, actor=actor)
            else:
                raise setup.SetupError("Unknown action.")
            setup.advance(conn, "roles", actor=actor)
    except (setup.SetupError, FolderError, PathError) as exc:
        return _private_page(request, error=str(exc))
    return redirect("/setup/roles")


# --- trash and backups ----------------------------------------------------------------


def _roles_page(request: Request, *, error: str | None = None) -> Response:
    return render(
        request,
        "setup/roles.html",
        {
            "step": "roles",
            "groups": _top_folders(request),
            "roles": [("trash", "Trash"), ("edit_backups", "Edit backups")],
            "error": error,
        },
        status=400 if error else 200,
    )


@router.get("/setup/roles")
def roles_page(request: Request) -> Response:
    return _open_step(request) or _roles_page(request)


@router.post("/setup/roles")
def apply_roles(
    request: Request,
    trash: Annotated[str, Form(max_length=4096)] = "",
    edit_backups: Annotated[str, Form(max_length=4096)] = "",
) -> Response:
    if (closed := _open_step(request)) is not None:
        return closed
    actor = _actor(request)
    try:
        chosen: dict[Role, FolderRef | None] = {
            "trash": FolderRef.parse(trash) if trash else None,
            "edit_backups": FolderRef.parse(edit_backups) if edit_backups else None,
        }
        with _runtime(request).dbs.state.write() as conn:
            setup.apply_roles(conn, chosen, actor=actor)
            setup.advance(conn, "done", actor=actor)
    except (setup.SetupError, FolderError, PathError) as exc:
        return _roles_page(request, error=str(exc))
    return redirect("/setup/done")


@router.get("/setup/done")
def done_page(request: Request) -> Response:
    progress = progress_of(request)
    if not progress.finished:
        return redirect(next_page(request))
    with _runtime(request).dbs.state.read() as conn:
        policy = load_policy(conn)
    return render(
        request,
        "setup/done.html",
        {
            "step": "done",
            "progress": progress,
            "libraries": sorted(policy.libraries.values(), key=lambda lib: lib.position),
            "mappings": sorted(
                (str(ref), m.type_name, m.permanent)
                for ref, m in policy.mappings.items()
                if m.type_name is not None
            ),
            "roles": sorted((role, str(ref)) for role, ref in policy.roles.items()),
        },
    )
