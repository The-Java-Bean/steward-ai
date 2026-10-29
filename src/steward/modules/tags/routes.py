# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`/ui/tags`: the vocabulary, review of AI-created tags, changes (each journaled, with
Undo) and re-tagging."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Form, Query
from fastapi.responses import Response

from steward.sdk import ActionFailed, ActionRefused, NotFound, TagInfo, WebDep

PAGE = "/ui/tags"
DONE = {
    "created": "Tag created.",
    "updated": "Tag changed.",
    "merged": "Tags merged.",
    "undo": "Undone.",
    "retag": "Tagging runs again for {n} file(s) in the background.",
    "retag_none": "Nothing to run again.",
}
"""Messages after a redirect (`?did=<key>&n=<count>`): fixed text, nothing reflected."""

TagForm = Annotated[str, Form(min_length=1, max_length=300)]
router = APIRouter()


@dataclass(frozen=True, slots=True)
class Row:
    name: str
    leaf: str
    depth: int
    tag: TagInfo | None
    """None: only a parent of other tags (not a tag itself)."""


def tree(tags: list[TagInfo]) -> list[Row]:
    """The tags by name, with the parents that aren't tags themselves added as rows."""
    by_name = {t.name: t for t in tags}
    names: dict[str, None] = {}
    for t in tags:
        parts = t.name.split("/")
        for i in range(1, len(parts) + 1):
            names.setdefault("/".join(parts[:i]))
    ordered = sorted(names, key=lambda n: [p.casefold() for p in n.split("/")])
    return [Row(n, n.rsplit("/", 1)[-1], n.count("/"), by_name.get(n)) for n in ordered]


def _redirect(web: WebDep, did: str, *, n: int | None = None, undo: int | None = None) -> Response:
    url = f"{PAGE}?did={did}"
    if n is not None:
        url += f"&n={n}"
    if undo is not None:
        url += f"&undo={undo}"
    return web.redirect(url)


def _page(web: WebDep, *, error: str | None = None, status: int = 200, **extra: object) -> Response:
    tags = web.tags.vocabulary()
    return web.render(
        "list.html",
        {
            "rows": tree(tags),
            "review": [t for t in tags if t.status == "ai-created"],
            "count": len(tags),
            "search": web.page_url("core.search") or "/ui/search",
            "error": error,
            "path": PAGE,
            **extra,
        },
        status=status,
    )


def _run(web: WebDep, name: str, params: dict[str, object], did: str) -> Response:
    try:
        done = web.actions.run(name, params)
    except (ActionRefused, ActionFailed) as exc:
        return _page(web, error=str(exc), status=409)
    return _redirect(web, did, undo=done.entry_id if done.undoable else None)


@router.get("")
def page(
    web: WebDep,
    did: Annotated[str, Query(max_length=16)] = "",
    n: Annotated[int, Query(ge=0, le=10_000_000)] = 0,
    undo: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    text = DONE.get(did)
    return _page(web, done_notice=text.format(n=n) if text else None, undo=undo)


@router.get("/edit")
def edit(web: WebDep, tag: Annotated[str, Query(min_length=1, max_length=300)]) -> Response:
    tags = web.tags.vocabulary(counts=False)
    found = next((t for t in tags if t.name == tag or tag in t.aliases), None)
    if found is None:
        raise NotFound(tag)
    others = [t for t in tags if t.name != found.name]
    return web.render("edit.html", {"tag": found, "others": others, "path": PAGE})


@router.post("/create")
def create(
    web: WebDep,
    name: TagForm,
    description: Annotated[str, Form(max_length=500)] = "",
) -> Response:
    return _run(web, "tag.create", {"name": name, "description": description}, "created")


@router.post("/update")
def update(
    web: WebDep,
    tag: TagForm,
    new_name: Annotated[str, Form(max_length=300)] = "",
    description: Annotated[str | None, Form(max_length=500)] = None,
    status: Annotated[str, Form(max_length=16)] = "",
) -> Response:
    params: dict[str, object] = {"tag": tag, "description": description}
    if new_name.strip():
        params["new_name"] = new_name
    if status in ("active", "deprecated"):
        params["status"] = status
    return _run(web, "tag.update", params, "updated")


@router.post("/merge")
def merge(web: WebDep, tag: TagForm, into: TagForm) -> Response:
    return _run(web, "tag.merge", {"tag": tag, "into": into}, "merged")


@router.post("/retag")
def retag(web: WebDep, everything: Annotated[bool, Form()] = False) -> Response:
    dropped = web.tags.retag(everything=everything)
    return _redirect(web, "retag" if dropped else "retag_none", n=dropped)


@router.post("/undo")
def undo(web: WebDep, entry: Annotated[int, Form(ge=1)]) -> Response:
    try:
        web.actions.undo(entry)
    except (ActionRefused, ActionFailed) as exc:
        return _page(web, error=str(exc), status=409)
    return _redirect(web, "undo")
