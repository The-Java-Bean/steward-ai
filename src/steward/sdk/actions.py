# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Action types: every change to files (or, later, to other programs) is one.

An action type turns parameters into a **plan** (what it will do, and the state the
files must be in for that to be right), applies the plan through the executor's
`GuardedFS`, and describes how to undo it (docs/adr/0005, plan §13).

- `plan(params, view)` has no side effects; `view` is read-only.
- The executor re-checks the plan's `preconditions` right before applying, under the
  single write lock; any drift fails the action without touching anything.
- `apply(plan, fs)` may only write through `fs`, which refuses paths outside the
  libraries, symlinks, non-writable folders and overwrites.
- `inverse(plan, applied, view)` returns the undo plan, or None if the action can't be
  undone. Its preconditions should be the state `apply` left (`applied.post`), so undo
  is refused once something else changed the files.
- `examples()` gives parameter sets for the executor-safety tests, which run against
  every registered action type on the `EXAMPLE_TREE` layout (library `lib`).

Paths are `library:relative/path` strings. Parameter fields holding one are typed
`LibraryPath`, so the safety tests can try hostile paths in each of them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from steward.sdk.stages import Sensitivity


class PathField:
    """Marks a parameter field as a library path (see `LibraryPath`)."""

    def __repr__(self) -> str:
        return "PathField()"


LibraryPath = Annotated[str, PathField()]
"""`library:relative/path`."""

NodeKind = Literal["file", "dir", "absent"]
AiMode = Literal["auto", "proposal"]


class ActionRefused(Exception):
    """The action isn't allowed (bad path, not writable, collision, preconditions not
    met...). Nothing was changed."""


class ActionFailed(Exception):
    """The action failed while applying (an OS error). The journal has no entry; files
    may be partly changed (an edit's backup copy, for example)."""


@dataclass(frozen=True, slots=True)
class PathState:
    """What is (or must be) at a path. `size`/`mtime_ns`/`content_hash` of None mean
    "don't care" in a precondition."""

    path: str
    kind: NodeKind
    size: int | None = None
    mtime_ns: int | None = None
    content_hash: str | None = None

    def satisfied_by(self, actual: PathState) -> bool:
        if actual.kind != self.kind:
            return False
        return all(
            want is None or want == got
            for want, got in (
                (self.size, actual.size),
                (self.mtime_ns, actual.mtime_ns),
                (self.content_hash, actual.content_hash),
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PathState:
        return cls(
            path=str(data["path"]),
            kind=data["kind"],
            size=data.get("size"),
            mtime_ns=data.get("mtime_ns"),
            content_hash=data.get("content_hash"),
        )


@dataclass(frozen=True, slots=True)
class Plan:
    action: str
    params: Mapping[str, Any]
    """JSON-compatible parameters (as validated by the action type's `params` model)."""
    summary: str
    """One line for people: "Move a/b.txt to c/b.txt"."""
    preconditions: tuple[PathState, ...] = ()
    reads: tuple[str, ...] = ()
    """Paths whose content the action reads or moves away (sources)."""
    writes: tuple[str, ...] = ()
    """Paths the action creates, changes, moves away or removes (folders included).
    `apply` may only write at these paths or inside them, and each must be in a
    writable folder."""
    data: Mapping[str, Any] = field(default_factory=dict)
    """Values resolved at planning time that `apply` needs (JSON-compatible)."""
    sensitive: bool = False
    """Sensitive with these parameters even though the action type isn't (a permanent
    lock, say). A plan can only add sensitivity, never remove it."""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "action": self.action,
            "params": dict(self.params),
            "summary": self.summary,
            "preconditions": [p.as_dict() for p in self.preconditions],
            "reads": list(self.reads),
            "writes": list(self.writes),
            "data": dict(self.data),
        }
        if self.sensitive:
            out["sensitive"] = True
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Plan:
        return cls(
            action=str(data["action"]),
            params=dict(data["params"]),
            summary=str(data["summary"]),
            preconditions=tuple(PathState.from_dict(p) for p in data.get("preconditions", [])),
            reads=tuple(data.get("reads", [])),
            writes=tuple(data.get("writes", [])),
            data=dict(data.get("data", {})),
            sensitive=bool(data.get("sensitive", False)),
        )


@dataclass(frozen=True, slots=True)
class Applied:
    result: Mapping[str, Any] = field(default_factory=dict)
    """What happened, for the journal (JSON-compatible, never file content)."""
    post: tuple[PathState, ...] = ()
    """The state the action left its paths in."""
    moves: tuple[tuple[str, str], ...] = ()
    """(source, destination) pairs, so files and folders keep their identity."""
    changed: tuple[str, ...] = ()
    """Paths whose content or existence changed; they are rescanned."""


@dataclass(frozen=True, slots=True)
class Rendered:
    summary: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PathPolicy:
    """Effective folder settings that matter for writing."""

    type: str
    writable: bool
    locked: bool
    sensitivity: Sensitivity
    lock_from: str | None = None
    """`library:path` of the folder whose lock applies here (itself or a parent)."""
    permanent: bool = False
    """The lock can only be removed on the host (`steward folders unlock --permanent`)."""


@runtime_checkable
class ReadOnlyFS(Protocol):
    """What an action type may look at while planning."""

    def state(self, path: str, *, content_hash: bool = False) -> PathState:
        """What's at `path` now (`absent` if nothing). A symlink, or a path through one,
        raises `ActionRefused`: steward-ai never acts on symlinks."""
        ...

    def settings(self, path: str) -> PathPolicy:
        """Settings of the folder `path` is in (or of `path` itself if it's a folder)."""
        ...

    def file_id(self, path: str) -> str | None:
        """The stable id discovery gave the file at `path`, if any."""
        ...

    def trash_folder(self, path: str) -> str:
        """The trash folder for something at `path` (same library, so the move is atomic)."""
        ...

    def backup_folder(self) -> str:
        """The `edit_backups` role folder."""
        ...

    def free_name(self, path: str) -> str:
        """`path` if nothing is there, else `name (2).ext`, `name (3).ext`..."""
        ...

    def now(self) -> datetime: ...


class GuardedFS(ReadOnlyFS, Protocol):
    """The only write handle. Built by the executor for one `apply`; every method checks
    the path rules again and never follows symlinks or overwrites."""

    def mkdir(self, path: str, *, parents: bool = False, mode: int = 0o755) -> list[str]:
        """Create a folder (and missing parents if asked). Returns the folders created.
        Refused if anything already exists at `path`."""
        ...

    def rmdir(self, path: str) -> None:
        """Remove an empty folder."""
        ...

    def move(self, source: str, destination: str, *, parents: bool = False) -> list[str]:
        """Atomic rename; refused if the destination exists. Returns the folders created
        above the destination (with `parents`), outermost first."""
        ...

    def create(self, path: str, data: bytes, *, mode: int = 0o644) -> None:
        """Write a new file (temp file, fsync, then link into place); never overwrites."""
        ...

    def copy(self, source: str, destination: str, *, parents: bool = False) -> list[str]:
        """Copy a file to a new path; never overwrites. Returns the folders created."""
        ...

    def replace(self, path: str, data: bytes) -> None:
        """Replace a file's content (temp file in the same folder, fsync, rename)."""
        ...

    def read(self, path: str, *, max_bytes: int) -> bytes: ...

    def set_lock(self, path: str, locked: bool, *, permanent: bool = False) -> None:
        """Lock or unlock a folder (folder settings, not the disk). `permanent` makes the
        lock removable only on the host; unlocking a permanent lock is refused."""
        ...

    def set_folder_type(self, path: str, type_name: str) -> None:
        """Map a folder to a folder type (used for folders steward-ai creates for itself,
        such as a library's trash)."""
        ...


class ActionType(Protocol):
    name: str
    """`file.move`; module action types are `<module>.<name>`."""
    params: type[BaseModel]
    ai_mode: AiMode
    """`auto`: an AI may run it directly (only additive or exposure-reducing actions).
    `proposal`: an AI can only propose it."""
    sensitive: bool
    """Needs a recent password check when a person runs or approves it. The executor
    also treats every action touching a locked folder as sensitive."""

    def plan(self, params: BaseModel, view: ReadOnlyFS) -> Plan: ...

    def render(self, plan: Plan) -> Rendered: ...

    def apply(self, plan: Plan, fs: GuardedFS) -> Applied: ...

    def inverse(self, plan: Plan, applied: Applied, view: ReadOnlyFS) -> Plan | None: ...

    def examples(self) -> list[dict[str, Any]]: ...


EXAMPLE_TREE: Mapping[str, str | None] = {
    "docs/a.txt": "alpha\n",
    "docs/b.md": "# bravo\n",
    "docs/nested/c.txt": "charlie\n",
    "photos/p.jpg": "\xff\xd8\xff fake jpeg",
    "empty": None,
}
"""The library (`lib`) the executor-safety tests build for `examples()`: relative path ->
file content, or None for an empty folder."""
