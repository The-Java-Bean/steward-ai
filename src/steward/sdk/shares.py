# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Public share links (plan §14.4, docs/adr/0020).

The core owns the only unauthenticated file routes, `/s/<token>` and
`/s/<token>/<path inside>`, and hands each request to the one module registered with
`Registry.share_links` (the built-in `shares` module, unless another provider replaces
it). The handler gets a `ShareRequest` and returns a response:

```python
def handle(share: ShareRequest) -> Response:
    row = find_share(share.state, share.token)       # the module's own tables
    if row is None:
        return share.not_found()
    item = share.open(row.path, row.kind)            # the core checks the item, every time
    return item.file_response(share.subpath)
```

`ShareRequest.open` is where the core enforces the sharing rules on every request: the
item must exist, be visible in normal views, not be locked, `local-only` or `excluded`,
and not be in a folder whose type isn't shareable. Everything a `SharedItem` returns
stays inside the item and leaves out what fails the same rules (a locked subfolder, a
file marked `local-only`). Paths in a `SharedItem` are relative to the item (`""` is
the item itself; `sub/file.txt` inside a shared folder), never `library:` paths, so a
public page never names anything outside what was shared.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from steward.sdk.web import Crumb, Entry, EntryKind

if TYPE_CHECKING:
    from fastapi.responses import Response

    from steward.sdk.state import StateAccess

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22}$")
"""Share tokens are 128 random bits, URL-safe base64 without padding
(`secrets.token_urlsafe(16)`). The core answers 404 to anything else."""


@dataclass(frozen=True, slots=True)
class SharedListing:
    """A folder inside a shared folder. `Entry.path` and `Crumb.path` are relative to the
    shared item."""

    sub: str
    name: str
    crumbs: tuple[Crumb, ...]
    """From the shared folder down to this folder's parent (empty at the top)."""
    entries: tuple[Entry, ...]
    """Folders first, then files, each by name."""
    truncated: bool = False

    @property
    def folders(self) -> tuple[Entry, ...]:
        return tuple(e for e in self.entries if e.kind == "dir")

    @property
    def files(self) -> tuple[Entry, ...]:
        return tuple(e for e in self.entries if e.kind == "file")


class SharedItem(Protocol):
    """A shared file or folder, checked by the core. Every method raises `NotFound` for
    a `sub` path that doesn't exist, leaves the item, or may not be shared."""

    @property
    def name(self) -> str: ...

    @property
    def kind(self) -> EntryKind: ...

    def entry(self, sub: str = "") -> Entry: ...

    def listing(self, sub: str = "", *, limit: int = 2000) -> SharedListing:
        """A folder of a shared folder (`""` = the shared folder itself)."""
        ...

    def read_text(self, sub: str = "", *, max_bytes: int) -> tuple[str, bool]:
        """The start of a file as text and whether it was cut."""
        ...

    def file_response(self, sub: str = "", *, inline: bool = False) -> Response:
        """A file's content, with byte ranges; `inline` shows safe types in the browser
        (same rules as `Files.file_response`)."""
        ...

    def zip_response(self, sub: str = "", *, name: str) -> Response:
        """A folder of the share (recursively) as one streamed zip."""
        ...


class ShareRequest(Protocol):
    """One request to `/s/<token>…` (see the module docstring)."""

    @property
    def token(self) -> str: ...

    @property
    def subpath(self) -> str:
        """What follows `/s/<token>/` (URL-decoded), `""` for the item itself."""
        ...

    @property
    def method(self) -> str: ...

    @property
    def query(self) -> Mapping[str, str]: ...

    @property
    def form(self) -> Mapping[str, str]:
        """Fields of a POSTed form (the core already checked its CSRF token)."""
        ...

    @property
    def client_ip(self) -> str:
        """The visitor's address (from `X-Forwarded-For` only behind a trusted proxy)."""
        ...

    @property
    def user_agent(self) -> str: ...

    @property
    def state(self) -> StateAccess: ...

    @property
    def form_token(self) -> str:
        """For the hidden `csrf_token` field of public forms (templates get it too)."""
        ...

    @property
    def base_url(self) -> str:
        """`/s/<token>`: links inside share pages start here."""
        ...

    def now(self) -> datetime: ...

    def cookie(self, name: str) -> str | None: ...

    def open(
        self, path: str, kind: EntryKind, *, only_files: Collection[str] | None = None
    ) -> SharedItem:
        """The item at `library:path`, if it exists as `kind` and may be shared right
        now; else `NotFound`. `only_files` (file IDs) limits a folder share to those
        files (a snapshot)."""
        ...

    def render(
        self,
        template: str,
        context: Mapping[str, object] | None = None,
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        """One of the module's templates (from its `ui_page(templates=...)`); public
        pages extend `public.html` (no navigation, nothing about the owner)."""
        ...

    def redirect(self, url: str) -> Response:
        """303 to a path under `base_url`."""
        ...

    def set_cookie(self, response: Response, name: str, value: str, *, max_age: int) -> None:
        """An HttpOnly cookie scoped to this share's path."""
        ...

    def not_found(self) -> Response:
        """The page every unknown, expired or revoked link gets (404)."""
        ...


type ShareLinkHandler = Callable[[ShareRequest], Response]
"""Answers requests to `/s/<token>…`; registered with `Registry.share_links`. Runs in a
worker thread (blocking calls are fine)."""
