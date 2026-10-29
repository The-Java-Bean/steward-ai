# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Web services for module pages (docs/adr/0017).

A route in a module's UI page asks for the per-request `Web` handle with the `WebDep`
dependency. Through it the module renders its templates inside the core layout, reads
files, and runs actions as the logged-in owner, without importing the core:

```python
@router.get("")
def page(web: WebDep, path: str = "") -> Response:
    listing = web.files.listing(path)
    return web.render("browse.html", {"listing": listing})
```

Everything here applies the same rules as the core's own pages: normal views never show
locked, hidden or trash folders (`NotFound`); the `locked` scope needs a recent password
check; sensitive actions ask for the password again (the core turns that into a redirect
to its re-auth page, so module code doesn't handle it).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol

from fastapi import Depends, Request

if TYPE_CHECKING:
    from fastapi.responses import Response

Scope = Literal["normal", "locked"]
"""`normal`: what every view shows. `locked`: only inside locked folders (re-auth)."""

EntryKind = Literal["file", "dir"]

type LinkBuilder = Callable[[str, EntryKind], str | None]
"""Turns a `library:path` into a link to a page showing it (or None). Registered with
`Registry.link_builder`; the core's pages (search, journal) use the first one."""


class NotFound(Exception):
    """No such file or folder, or not visible in this scope (answered with 404)."""


@dataclass(frozen=True, slots=True)
class Viewer:
    """Who is logged in."""

    username: str
    actor: str
    """What the journal records (`user:<name>`)."""


@dataclass(frozen=True, slots=True)
class LibraryInfo:
    library_id: str
    name: str

    @property
    def path(self) -> str:
        return f"{self.library_id}:"


@dataclass(frozen=True, slots=True)
class Crumb:
    name: str
    path: str


@dataclass(frozen=True, slots=True)
class Entry:
    """A file or folder in a listing."""

    name: str
    path: str
    """`library:relative/path`."""
    kind: EntryKind
    size: int | None = None
    modified: datetime | None = None
    file_id: str | None = None
    """Set once discovery has seen the file."""
    mime: str | None = None
    file_kind: str | None = None
    """`document`, `image`, `video`, `audio`, `text`, `archive` or `other`."""
    locked: bool = False
    """A folder that is locked (only listed in the `locked` scope)."""


@dataclass(frozen=True, slots=True)
class FolderListing:
    path: str
    library_id: str
    relpath: str
    name: str
    crumbs: tuple[Crumb, ...]
    """From the library root down to this folder's parent."""
    entries: tuple[Entry, ...]
    """Folders first, then files, each by name."""
    type_name: str
    writable: bool
    shareable: bool
    locked: bool
    permanent: bool
    truncated: bool = False
    """More entries than the listing's limit."""
    lock_from: str | None = None
    """`library:path` of the folder whose lock applies (this one or a parent)."""

    @property
    def folders(self) -> tuple[Entry, ...]:
        return tuple(e for e in self.entries if e.kind == "dir")

    @property
    def files(self) -> tuple[Entry, ...]:
        return tuple(e for e in self.entries if e.kind == "file")


@dataclass(frozen=True, slots=True)
class FilePage:
    """A page of files found across the libraries (by kind or by a search)."""

    entries: tuple[Entry, ...]
    total: int
    capped: bool
    """`total` stopped counting (there are more)."""


KindPage = FilePage
"""The name `by_kind` used first."""


@dataclass(frozen=True, slots=True)
class LockedFolder:
    """A folder the Locked Folder view starts from (an outermost locked folder)."""

    path: str
    name: str
    library_name: str
    permanent: bool
    type_name: str
    """Its folder type; a locked type (such as `private`) locks it by itself."""


@dataclass(frozen=True, slots=True)
class UnlockPreview:
    """What unlocking a folder would make visible in normal views."""

    path: str
    allowed: bool
    reason: str | None
    """Why unlocking isn't possible (permanent, or locked by a parent)."""
    files: int
    folders: int
    size: int
    """Bytes in `files`. Counts cover what steward-ai has scanned."""
    sample: tuple[str, ...]
    """Some of the paths that would become visible."""
    still_locked: tuple[str, ...]
    """Folders inside that keep their own lock."""
    type_locks: bool
    """The folder's type locks it; unlocking overrides the type for this folder."""


class Files(Protocol):
    """Reading files and folders (writing goes through `Actions`)."""

    def libraries(self) -> list[LibraryInfo]: ...

    def listing(self, path: str, *, scope: Scope = "normal", limit: int = 5000) -> FolderListing:
        """A folder's subfolders and files. Raises `NotFound`."""
        ...

    def entry(self, path: str, *, scope: Scope = "normal") -> Entry:
        """One file or folder. Raises `NotFound`."""
        ...

    def by_kind(
        self,
        kind: str,
        *,
        library: str | None = None,
        scope: Scope = "normal",
        limit: int = 100,
        offset: int = 0,
    ) -> FilePage:
        """Files of one kind, newest first."""
        ...

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
        """Files whose path contains every word of `query`. Raises `ValueError` for a
        query that can't be searched (too many or too long words)."""
        ...

    def locked_folders(self) -> list[LockedFolder]:
        """The locked folders, by library and path (needs a recent password check)."""
        ...

    def unlock_preview(self, path: str) -> UnlockPreview:
        """What unlocking the locked folder `path` would expose. Raises `NotFound`."""
        ...

    def read_text(self, path: str, *, scope: Scope = "normal", max_bytes: int) -> tuple[str, bool]:
        """The start of a file as text (invalid UTF-8 replaced) and whether it was cut."""
        ...

    def file_response(
        self, path: str, *, scope: Scope = "normal", inline: bool = False
    ) -> Response:
        """The file's content, with byte ranges (`Range`) for media players. `inline`
        shows images, PDFs, audio, video and plain text in the browser; everything else
        downloads. Content is served with a sandboxing security policy."""
        ...

    def zip_response(self, paths: Sequence[str], *, scope: Scope = "normal", name: str) -> Response:
        """Files and folders (recursively) as one streamed zip, built as it downloads.
        Folders hidden in `scope` are left out."""
        ...


@dataclass(frozen=True, slots=True)
class ActionPreview:
    summary: str
    details: tuple[str, ...]
    sensitive: bool


@dataclass(frozen=True, slots=True)
class ActionDone:
    entry_id: int
    summary: str
    undoable: bool


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """One step of `Actions.run_all`: `done` or `error` is set."""

    name: str
    params: Mapping[str, Any]
    done: ActionDone | None = None
    error: str | None = None


class Actions(Protocol):
    """Running action types as the logged-in owner (journaled, undoable)."""

    def preview(self, name: str, params: Mapping[str, Any]) -> ActionPreview:
        """Raises `ActionRefused`."""
        ...

    def run(self, name: str, params: Mapping[str, Any]) -> ActionDone:
        """Raises `ActionRefused` (nothing changed) or `ActionFailed`. A sensitive action
        asks for the password first when the last check isn't recent."""
        ...

    def run_all(self, steps: Sequence[tuple[str, Mapping[str, Any]]]) -> list[StepOutcome]:
        """Several actions, one password check for all of them if any is sensitive. Each
        step stands alone: a refused step doesn't stop the others."""
        ...

    def undo(self, entry_id: int) -> ActionDone:
        """Raises `ActionRefused` or `ActionFailed`."""
        ...


class Web(Protocol):
    """The per-request handle (see the module docstring)."""

    @property
    def viewer(self) -> Viewer: ...

    @property
    def module(self) -> str:
        """The module whose page this request is for."""
        ...

    @property
    def files(self) -> Files: ...

    @property
    def actions(self) -> Actions: ...

    @property
    def csrf_token(self) -> str:
        """For forms (`<input type="hidden" name="csrf_token">`) and the `X-CSRF-Token`
        header. Templates also get it as `form_token`."""
        ...

    @property
    def static_url(self) -> str:
        """Where the module's static files are served (`/static/m/<module>`)."""
        ...

    def render(
        self,
        template: str,
        context: Mapping[str, Any] | None = None,
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        """Render one of the module's templates (from `ui_page(templates=...)`). They may
        `{% extends "base.html" %}` and fill `title` and `content`; context values
        `notice` and `error` show as messages."""
        ...

    def redirect(self, url: str) -> Response:
        """303 to a same-site path (anything else goes to `/ui/`)."""
        ...

    def recently_authenticated(self) -> bool:
        """Whether a sensitive step would pass now without asking for the password."""
        ...

    def require_reauth(self) -> None:
        """Ask for the password unless it was entered recently (the request ends in a
        redirect to the re-auth page, which comes back here)."""
        ...


def get_web(request: Request) -> Web:
    """The FastAPI dependency behind `WebDep`."""
    factory: Callable[[Request], Web] | None = getattr(request.app.state, "steward_web", None)
    if factory is None:
        raise RuntimeError("steward web services exist only inside steward-api")
    return factory(request)


WebDep = Annotated[Web, Depends(get_web)]
