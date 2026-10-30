# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The core's action types: folders, moves, renames, delete-to-trash, new files, edits,
locks (plan §13.2). Uploads come with the file manager, shares and tags with their
modules."""

from __future__ import annotations

import re
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from steward.core.executor.guarded import UPLOAD_PREFIX, content_hash, within
from steward.sdk import (
    SIDECAR_SUFFIX,
    ActionRefused,
    AiMode,
    Applied,
    GuardedFS,
    LibraryPath,
    PathState,
    Plan,
    ReadOnlyFS,
    Registry,
    Rendered,
)

MAX_TEXT_BYTES = 16 * 1024 * 1024
"""Largest file `file.create` writes and `file.edit` reads or writes."""


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _name(path: str) -> str:
    return path.rsplit("/", 1)[-1] if "/" in path else path.partition(":")[2]


def _parent(path: str) -> str:
    library, _, relpath = path.partition(":")
    return f"{library}:{relpath.rsplit('/', 1)[0] if '/' in relpath else ''}"


def _join(folder: str, relpath: str) -> str:
    return folder + relpath if folder.endswith(":") else f"{folder}/{relpath}"


def _existing(view: ReadOnlyFS, path: str, *, content: bool = False) -> PathState:
    state = view.state(path, content_hash=content)
    if state.kind == "absent":
        raise ActionRefused(f"{path} doesn't exist")
    return state


def _absent(view: ReadOnlyFS, path: str) -> PathState:
    state = view.state(path)
    if state.kind != "absent":
        raise ActionRefused(f"{path} already exists")
    return state


class _Action:
    name: ClassVar[str]
    params: ClassVar[type[BaseModel]]
    ai_mode: ClassVar[AiMode] = "proposal"
    sensitive: ClassVar[bool] = False

    def render(self, plan: Plan) -> Rendered:
        return Rendered(plan.summary)

    def examples(self) -> list[dict[str, Any]]:
        return []


# --- folders ---------------------------------------------------------------------------


class MkdirParams(_Params):
    path: LibraryPath
    parents: bool = False
    private: bool = False
    """Create it readable by steward-ai's user only (mode 0700)."""


class Mkdir(_Action):
    name = "file.mkdir"
    params = MkdirParams
    ai_mode = "auto"  # additive

    def plan(self, params: BaseModel, view: ReadOnlyFS) -> Plan:
        assert isinstance(params, MkdirParams)  # noqa: S101
        return Plan(
            self.name,
            params.model_dump(mode="json"),
            f"Create folder {params.path}",
            preconditions=(_absent(view, params.path),),
            writes=(params.path,),
        )

    def apply(self, plan: Plan, fs: GuardedFS) -> Applied:
        p = MkdirParams.model_validate(plan.params)
        created = fs.mkdir(p.path, parents=p.parents, mode=0o700 if p.private else 0o755)
        return Applied(
            {"created": created},
            post=tuple(PathState(c, "dir") for c in created),
            changed=(created[0],),
        )

    def inverse(self, plan: Plan, applied: Applied, view: ReadOnlyFS) -> Plan | None:
        created = list(applied.result["created"])
        return Plan(
            Rmdir.name,
            {"paths": list(reversed(created))},
            f"Remove folder {created[-1]}"
            if len(created) == 1
            else f"Remove {len(created)} folders",
            preconditions=tuple(PathState(c, "dir") for c in created),
            writes=tuple(created),
        )

    def examples(self) -> list[dict[str, Any]]:
        return [{"path": "lib:docs/new"}, {"path": "lib:x/y/z", "parents": True}]


class RmdirParams(_Params):
    paths: list[LibraryPath] = Field(min_length=1, max_length=1000)
    """Empty folders, innermost first."""


class Rmdir(_Action):
    name = "file.rmdir"
    params = RmdirParams

    def plan(self, params: BaseModel, view: ReadOnlyFS) -> Plan:
        assert isinstance(params, RmdirParams)  # noqa: S101
        states = []
        for path in params.paths:
            state = _existing(view, path)
            if state.kind != "dir":
                raise ActionRefused(f"{path} isn't a folder")
            states.append(state)
        return Plan(
            self.name,
            params.model_dump(mode="json"),
            f"Remove empty folder {params.paths[0]}"
            if len(params.paths) == 1
            else f"Remove {len(params.paths)} empty folders",
            preconditions=tuple(states),
            writes=tuple(params.paths),
        )

    def apply(self, plan: Plan, fs: GuardedFS) -> Applied:
        p = RmdirParams.model_validate(plan.params)
        for path in p.paths:
            fs.rmdir(path)
        return Applied(
            {"removed": list(p.paths)},
            post=tuple(PathState(path, "absent") for path in p.paths),
            changed=tuple(p.paths),
        )

    def inverse(self, plan: Plan, applied: Applied, view: ReadOnlyFS) -> Plan | None:
        paths = list(reversed(applied.result["removed"]))  # outermost first
        return Plan(
            Mkdirs.name,
            {"paths": paths},
            f"Recreate folder {paths[-1]}" if len(paths) == 1 else f"Recreate {len(paths)} folders",
            preconditions=tuple(PathState(path, "absent") for path in paths),
            writes=tuple(paths),
        )

    def examples(self) -> list[dict[str, Any]]:
        return [{"paths": ["lib:empty"]}]


class MkdirsParams(_Params):
    paths: list[LibraryPath] = Field(min_length=1, max_length=1000)
    """Outermost first; each one's parent exists or is earlier in the list."""


class Mkdirs(_Action):
    """Recreate several folders (the undo of `file.rmdir`)."""

    name = "file.mkdirs"
    params = MkdirsParams
    ai_mode = "auto"

    def plan(self, params: BaseModel, view: ReadOnlyFS) -> Plan:
        assert isinstance(params, MkdirsParams)  # noqa: S101
        return Plan(
            self.name,
            params.model_dump(mode="json"),
            f"Create {len(params.paths)} folder(s)",
            preconditions=tuple(_absent(view, path) for path in params.paths),
            writes=tuple(params.paths),
        )

    def apply(self, plan: Plan, fs: GuardedFS) -> Applied:
        p = MkdirsParams.model_validate(plan.params)
        created = [c for path in p.paths for c in fs.mkdir(path)]
        return Applied(
            {"created": created},
            post=tuple(PathState(c, "dir") for c in created),
            changed=tuple(created),
        )

    def inverse(self, plan: Plan, applied: Applied, view: ReadOnlyFS) -> Plan | None:
        created = list(applied.result["created"])
        return Plan(
            Rmdir.name,
            {"paths": list(reversed(created))},
            f"Remove {len(created)} folder(s)",
            preconditions=tuple(PathState(c, "dir") for c in created),
            writes=tuple(created),
        )

    def examples(self) -> list[dict[str, Any]]:
        return [{"paths": ["lib:new", "lib:new/inner"]}]


# --- moves -----------------------------------------------------------------------------


class MoveParams(_Params):
    source: LibraryPath
    destination: LibraryPath
    parents: bool = False
    """Create missing folders above the destination."""
    remove_empty: list[LibraryPath] = Field(default_factory=list, max_length=1000)
    """Folders to remove afterwards if they're empty, innermost first (an undo removes
    the folders the original move created)."""


def _cleanup(params: BaseModel) -> tuple[str, ...]:
    return tuple(getattr(params, "remove_empty", ()))


def _sidecar(view: ReadOnlyFS, source: PathState) -> PathState | None:
    """The XMP sidecar next to the file `source`, if there is one. It moves with the file
    (also ones other programs wrote): left behind, it would keep a photo's tags and
    description in the open after the photo moved into a locked folder."""
    if source.kind != "file":
        return None
    state = view.state(source.path + SIDECAR_SUFFIX)
    return state if state.kind == "file" else None


def _move_plan(
    name: str, params: BaseModel, source: str, destination: str, view: ReadOnlyFS, summary: str
) -> Plan:
    if source == destination:
        raise ActionRefused("the source and the destination are the same")
    if source.partition(":")[2] == "":
        raise ActionRefused("a library root can't be moved")
    src = _existing(view, source)
    preconditions = [src, _absent(view, destination)]
    pair: tuple[str, ...] = ()
    if (sidecar := _sidecar(view, src)) is not None:
        target = destination + SIDECAR_SUFFIX
        preconditions += [sidecar, _absent(view, target)]
        pair = (sidecar.path, target)
    return Plan(
        name,
        params.model_dump(mode="json"),
        summary,
        preconditions=tuple(preconditions),
        reads=(source, *pair[:1]),
        writes=(source, destination, *pair, *_cleanup(params)),
        data={"sidecar": list(pair)} if pair else {},
    )


def _move_with_sidecar(
    plan: Plan, fs: GuardedFS, source: str, destination: str, *, parents: bool
) -> list[str]:
    """The sidecar goes first: if either move can't work (another disk, say), nothing has
    moved yet."""
    pair = plan.data.get("sidecar")
    created = fs.move(pair[0], pair[1], parents=parents) if pair else []
    return created + fs.move(source, destination, parents=parents)


def _moved(
    plan: Plan,
    source: str,
    destination: str,
    fs: GuardedFS,
    created: list[str],
    remove_empty: tuple[str, ...] = (),
) -> Applied:
    removed = []
    for folder in remove_empty:
        try:
            fs.rmdir(folder)
        except ActionRefused:
            continue  # not empty any more: keep it
        removed.append(folder)
    moves = [(source, destination)]
    result = {"source": source, "destination": destination, "created": created, "removed": removed}
    if pair := plan.data.get("sidecar"):
        moves.append((pair[0], pair[1]))
        result["sidecar"] = list(pair)
    return Applied(
        result,
        post=tuple(p for s, d in moves for p in (PathState(s, "absent"), fs.state(d))),
        moves=tuple(moves),
        changed=(*(p for move in moves for p in move), *removed),
    )


def _move_back(applied: Applied) -> Plan:
    source, destination = applied.result["source"], applied.result["destination"]
    after = {p.path: p for p in applied.post}
    created = list(reversed(applied.result.get("created", [])))
    preconditions = [after[destination], PathState(source, "absent")]
    pair: tuple[str, ...] = ()
    if sidecar := applied.result.get("sidecar"):
        preconditions += [after[sidecar[1]], PathState(sidecar[0], "absent")]
        pair = (sidecar[1], sidecar[0])
    return Plan(
        Move.name,
        {
            "source": destination,
            "destination": source,
            "parents": True,
            "remove_empty": created,
        },
        f"Move {destination} back to {source}",
        preconditions=tuple(preconditions),
        reads=(destination, *pair[:1]),
        writes=(destination, source, *pair, *created),
        data={"sidecar": list(pair)} if pair else {},
    )


class Move(_Action):
    name = "file.move"
    params = MoveParams

    def plan(self, params: BaseModel, view: ReadOnlyFS) -> Plan:
        assert isinstance(params, MoveParams)  # noqa: S101
        return _move_plan(
            self.name,
            params,
            params.source,
            params.destination,
            view,
            f"Move {params.source} to {params.destination}",
        )

    def apply(self, plan: Plan, fs: GuardedFS) -> Applied:
        p = MoveParams.model_validate(plan.params)
        created = _move_with_sidecar(plan, fs, p.source, p.destination, parents=p.parents)
        return _moved(plan, p.source, p.destination, fs, created, tuple(p.remove_empty))

    def inverse(self, plan: Plan, applied: Applied, view: ReadOnlyFS) -> Plan | None:
        return _move_back(applied)

    def examples(self) -> list[dict[str, Any]]:
        return [
            {"source": "lib:docs/a.txt", "destination": "lib:photos/a.txt"},
            {"source": "lib:docs/nested", "destination": "lib:moved/nested", "parents": True},
            {"source": "lib:photos/p.jpg", "destination": "lib:moved/p.jpg", "parents": True},
        ]


class RenameParams(_Params):
    path: LibraryPath
    new_name: str = Field(min_length=1, max_length=255)

    @field_validator("new_name")
    @classmethod
    def _one_name(cls, value: str) -> str:
        if "/" in value or "\\" in value or value in (".", ".."):
            raise ValueError("a new name can't contain '/' or '\\' or be '.' or '..'")
        return value


class Rename(_Action):
    name = "file.rename"
    params = RenameParams

    def plan(self, params: BaseModel, view: ReadOnlyFS) -> Plan:
        assert isinstance(params, RenameParams)  # noqa: S101
        destination = _join(_parent(params.path), params.new_name)
        return _move_plan(
            self.name,
            params,
            params.path,
            destination,
            view,
            f"Rename {params.path} to {params.new_name}",
        )

    def apply(self, plan: Plan, fs: GuardedFS) -> Applied:
        p = RenameParams.model_validate(plan.params)
        destination = _join(_parent(p.path), p.new_name)
        _move_with_sidecar(plan, fs, p.path, destination, parents=False)
        return _moved(plan, p.path, destination, fs, [])

    def inverse(self, plan: Plan, applied: Applied, view: ReadOnlyFS) -> Plan | None:
        return _move_back(applied)

    def examples(self) -> list[dict[str, Any]]:
        return [
            {"path": "lib:docs/b.md", "new_name": "renamed.md"},
            {"path": "lib:docs/nested", "new_name": "deeper"},
            {"path": "lib:photos/p.jpg", "new_name": "renamed.jpg"},
        ]


class DeleteParams(_Params):
    path: LibraryPath


class Delete(_Action):
    """Move a file or folder to the trash. steward-ai never empties the trash itself."""

    name = "file.delete"
    params = DeleteParams
    sensitive = True

    def plan(self, params: BaseModel, view: ReadOnlyFS) -> Plan:
        assert isinstance(params, DeleteParams)  # noqa: S101
        source = _existing(view, params.path)
        library, _, relpath = params.path.partition(":")
        if relpath == "":
            raise ActionRefused("a library root can't be deleted")
        trash = view.trash_folder(params.path)
        if within(params.path, trash) or within(trash, params.path):
            raise ActionRefused("the trash itself can't be moved to the trash")
        root = view.trash_folder(f"{library}:")
        day = view.now().strftime("%Y-%m-%d")
        destination = view.free_name(_join(trash, f"{day}/{relpath}"))
        preconditions = [source, PathState(destination, "absent")]
        pair: tuple[str, ...] = ()
        if (sidecar := _sidecar(view, source)) is not None:
            target = view.free_name(destination + SIDECAR_SUFFIX)
            preconditions += [sidecar, PathState(target, "absent")]
            pair = (sidecar.path, target)
        return Plan(
            self.name,
            params.model_dump(mode="json"),
            f"Move {params.path} to the trash",
            preconditions=tuple(preconditions),
            reads=(params.path, *pair[:1]),
            writes=(params.path, *pair[:1], root),
            data={
                "trash": trash,
                "destination": destination,
                "locked_trash": view.settings(params.path).locked,
                "trash_root": root,
                **({"sidecar": list(pair)} if pair else {}),
            },
        )

    def apply(self, plan: Plan, fs: GuardedFS) -> Applied:
        p = DeleteParams.model_validate(plan.params)
        trash, destination = str(plan.data["trash"]), str(plan.data["destination"])
        root = str(plan.data["trash_root"])
        if fs.state(root).kind == "absent":
            fs.mkdir(root, parents=True)
            fs.set_folder_type(root, "trash")
        if plan.data.get("locked_trash"):
            if fs.state(trash).kind == "absent":
                fs.mkdir(trash, parents=True, mode=0o700)
            fs.set_lock(trash, True)
        created = _move_with_sidecar(plan, fs, p.path, destination, parents=True)
        return _moved(plan, p.path, destination, fs, created)

    def inverse(self, plan: Plan, applied: Applied, view: ReadOnlyFS) -> Plan | None:
        undo = _move_back(applied)
        return Plan(
            undo.action,
            undo.params,
            f"Restore {applied.result['source']} from the trash",
            preconditions=undo.preconditions,
            reads=undo.reads,
            writes=undo.writes,
            data=undo.data,
        )

    def examples(self) -> list[dict[str, Any]]:
        return [
            {"path": "lib:docs/a.txt"},
            {"path": "lib:docs/nested"},
            {"path": "lib:photos/p.jpg"},
        ]


# --- file content ------------------------------------------------------------------------


class CreateParams(_Params):
    path: LibraryPath
    content: str = Field(max_length=MAX_TEXT_BYTES)


class Create(_Action):
    """Write a new text file. Never overwrites."""

    name = "file.create"
    params = CreateParams
    ai_mode = "proposal"

    def plan(self, params: BaseModel, view: ReadOnlyFS) -> Plan:
        assert isinstance(params, CreateParams)  # noqa: S101
        if len(params.content.encode()) > MAX_TEXT_BYTES:
            raise ActionRefused("the content is too large")
        return Plan(
            self.name,
            params.model_dump(mode="json"),
            f"Create {params.path}",
            preconditions=(_absent(view, params.path),),
            writes=(params.path,),
        )

    def apply(self, plan: Plan, fs: GuardedFS) -> Applied:
        p = CreateParams.model_validate(plan.params)
        fs.create(p.path, p.content.encode())
        return Applied(
            {"path": p.path},
            post=(fs.state(p.path, content_hash=True),),
            changed=(p.path,),
        )

    def inverse(self, plan: Plan, applied: Applied, view: ReadOnlyFS) -> Plan | None:
        path = str(applied.result["path"])
        trash = Delete().plan(DeleteParams(path=path), view)
        return Plan(
            trash.action,
            trash.params,
            f"Move {path} (created) to the trash",
            preconditions=(applied.post[0], *trash.preconditions[1:]),
            reads=trash.reads,
            writes=trash.writes,
            data=trash.data,
        )

    def examples(self) -> list[dict[str, Any]]:
        return [
            {"path": "lib:docs/new.txt", "content": "new\n"},
            {"path": "lib:empty/note.md", "content": "# note\n"},
        ]


UPLOAD_TEMP = re.compile(rf"^{re.escape(UPLOAD_PREFIX)}[0-9a-f]{{32}}$")


class UploadParams(_Params):
    temp: LibraryPath
    """The finished upload's temporary file, in the same folder as `path`."""
    path: LibraryPath
    size: int = Field(ge=0)


class Upload(_Action):
    """Put a finished upload in place (`core.executor.uploads` wrote the bytes into a
    hidden temporary file next to it). Never overwrites; undo moves it to the trash."""

    name = "file.upload"
    params = UploadParams
    ai_mode = "proposal"

    def plan(self, params: BaseModel, view: ReadOnlyFS) -> Plan:
        assert isinstance(params, UploadParams)  # noqa: S101
        if not UPLOAD_TEMP.match(_name(params.temp)) or _parent(params.temp) != _parent(
            params.path
        ):
            raise ActionRefused("not an upload in the destination folder")
        if _name(params.path).startswith(".steward-tmp-"):
            raise ActionRefused("that name is reserved for steward-ai's temporary files")
        temp = _existing(view, params.temp)
        if temp.kind != "file" or temp.size != params.size:
            raise ActionRefused("the upload isn't complete")
        return Plan(
            self.name,
            params.model_dump(mode="json"),
            f"Upload {params.path}",
            preconditions=(PathState(params.temp, "file", params.size), _absent(view, params.path)),
            reads=(params.temp,),
            writes=(params.temp, params.path),
        )

    def apply(self, plan: Plan, fs: GuardedFS) -> Applied:
        p = UploadParams.model_validate(plan.params)
        fs.move(p.temp, p.path)
        # No content hash: re-reading a large upload here would hold the write lock for
        # a long time. Size and modification time catch later changes.
        return Applied({"path": p.path}, post=(fs.state(p.path),), changed=(p.path,))

    def inverse(self, plan: Plan, applied: Applied, view: ReadOnlyFS) -> Plan | None:
        path = str(applied.result["path"])
        trash = Delete().plan(DeleteParams(path=path), view)
        return Plan(
            trash.action,
            trash.params,
            f"Move {path} (uploaded) to the trash",
            preconditions=(applied.post[0], *trash.preconditions[1:]),
            reads=trash.reads,
            writes=trash.writes,
            data=trash.data,
        )


class EditParams(_Params):
    path: LibraryPath
    content: str | None = Field(None, max_length=MAX_TEXT_BYTES)
    """New text content..."""
    copy_from: LibraryPath | None = None
    """...or another file's content (used to restore an edit backup)."""
    expected_hash: str | None = None
    """The content hash the file must still have (refused if it changed)."""

    @model_validator(mode="after")
    def _one_source(self) -> EditParams:
        if (self.content is None) == (self.copy_from is None):
            raise ValueError("give either content or copy_from")
        return self


class Edit(_Action):
    """Replace a file's content. The old version is copied to the edit-backups folder
    first; the new one is written to a temp file and renamed over the original."""

    name = "file.edit"
    params = EditParams

    def plan(self, params: BaseModel, view: ReadOnlyFS) -> Plan:
        assert isinstance(params, EditParams)  # noqa: S101
        current = _existing(view, params.path, content=True)
        if current.kind != "file":
            raise ActionRefused(f"{params.path} isn't a file")
        if current.size is not None and current.size > MAX_TEXT_BYTES:
            raise ActionRefused(f"{params.path} is too large to edit")
        if params.expected_hash is not None and params.expected_hash != current.content_hash:
            raise ActionRefused(f"{params.path} changed since it was read")
        preconditions = [current]
        reads = [params.path]
        if params.copy_from is not None:
            source = _existing(view, params.copy_from, content=True)
            if source.kind != "file":
                raise ActionRefused(f"{params.copy_from} isn't a file")
            preconditions.append(source)
            reads.append(params.copy_from)
        elif params.content is not None and len(params.content.encode()) > MAX_TEXT_BYTES:
            raise ActionRefused("the content is too large")
        now = view.now()
        backups = view.backup_folder()
        file_key = view.file_id(params.path) or "unknown"
        backup = _join(
            backups,
            f"{now:%Y}/{now:%m}/{file_key}/{now:%Y%m%dT%H%M%S%f}-{_name(params.path)}",
        )
        preconditions.append(PathState(backup, "absent"))
        return Plan(
            self.name,
            params.model_dump(mode="json"),
            f"Edit {params.path}",
            preconditions=tuple(preconditions),
            reads=tuple(reads),
            writes=(params.path, backups),
            data={"backup": backup},
        )

    def apply(self, plan: Plan, fs: GuardedFS) -> Applied:
        p = EditParams.model_validate(plan.params)
        backup = str(plan.data["backup"])
        fs.copy(p.path, backup, parents=True)
        if p.copy_from is not None:
            data = fs.read(p.copy_from, max_bytes=MAX_TEXT_BYTES)
        else:
            data = (p.content or "").encode()
        fs.replace(p.path, data)
        after = fs.state(p.path, content_hash=True)
        if after.content_hash != content_hash(data):
            raise ActionRefused(f"{p.path} changed while it was being written")
        return Applied(
            {"path": p.path, "backup": backup},
            post=(after, fs.state(backup, content_hash=True)),
            changed=(p.path, backup),
        )

    def inverse(self, plan: Plan, applied: Applied, view: ReadOnlyFS) -> Plan | None:
        path, backup = str(applied.result["path"]), str(applied.result["backup"])
        undo = self.plan(EditParams(path=path, copy_from=backup), view)
        return Plan(
            undo.action,
            undo.params,
            f"Restore the previous version of {path}",
            preconditions=(applied.post[0], *undo.preconditions[1:]),
            reads=undo.reads,
            writes=undo.writes,
            data=undo.data,
        )

    def examples(self) -> list[dict[str, Any]]:
        return [
            {"path": "lib:docs/a.txt", "content": "alpha, edited\n"},
            {"path": "lib:docs/b.md", "copy_from": "lib:docs/nested/c.txt"},
        ]


# --- locks -------------------------------------------------------------------------------


class LockParams(_Params):
    path: LibraryPath
    permanent: bool = False
    """Only removable on the host afterwards (`steward folders unlock --permanent`)."""


class UnlockParams(_Params):
    path: LibraryPath


class Lock(_Action):
    """Lock a folder: hide it everywhere except the Locked Folder view. With `permanent`
    the lock can't be removed from the UI or API (and can't be undone); making a lock
    permanent is sensitive. A folder locked here already can be made permanent."""

    name = "folder.lock"
    params = LockParams
    ai_mode = "auto"  # reduces exposure

    def plan(self, params: BaseModel, view: ReadOnlyFS) -> Plan:
        assert isinstance(params, LockParams)  # noqa: S101
        state = _existing(view, params.path)
        if state.kind != "dir":
            raise ActionRefused(f"{params.path} isn't a folder")
        current = view.settings(params.path)
        if current.locked and not params.permanent:
            raise ActionRefused(f"{params.path} is already locked")
        if current.locked and current.permanent:
            raise ActionRefused(f"{params.path} is already permanently locked")
        if current.locked and current.lock_from != params.path:
            raise ActionRefused(
                f"{params.path} is locked because {current.lock_from} is; "
                "make that lock permanent instead"
            )
        what = "Lock folder" if not current.locked else "Make the lock permanent on"
        suffix = " permanently" if params.permanent and not current.locked else ""
        return Plan(
            self.name,
            params.model_dump(mode="json"),
            f"{what} {params.path}{suffix}",
            preconditions=(state,),
            sensitive=params.permanent,
        )

    def apply(self, plan: Plan, fs: GuardedFS) -> Applied:
        p = LockParams.model_validate(plan.params)
        fs.set_lock(p.path, True, permanent=p.permanent)
        return Applied({"path": p.path, "permanent": p.permanent}, post=(fs.state(p.path),))

    def inverse(self, plan: Plan, applied: Applied, view: ReadOnlyFS) -> Plan | None:
        if applied.result.get("permanent"):
            return None  # only `steward folders unlock --permanent` removes it
        path = str(applied.result["path"])
        return Plan(
            Unlock.name,
            {"path": path},
            f"Unlock folder {path}",
            preconditions=(PathState(path, "dir"),),
        )

    def examples(self) -> list[dict[str, Any]]:
        return [{"path": "lib:photos"}, {"path": "lib:docs", "permanent": True}]


class Unlock(_Action):
    """Unlock a folder. Refused for permanent locks and for locks set on a parent."""

    name = "folder.unlock"
    params = UnlockParams
    sensitive = True

    def plan(self, params: BaseModel, view: ReadOnlyFS) -> Plan:
        assert isinstance(params, UnlockParams)  # noqa: S101
        state = _existing(view, params.path)
        if state.kind != "dir":
            raise ActionRefused(f"{params.path} isn't a folder")
        current = view.settings(params.path)
        if not current.locked:
            raise ActionRefused(f"{params.path} isn't locked")
        if current.lock_from != params.path:
            raise ActionRefused(
                f"{params.path} is locked because {current.lock_from} is; unlock that instead"
            )
        if current.permanent:
            raise ActionRefused(
                f"{params.path} is permanently locked; only `steward folders unlock "
                "--permanent` on the server can unlock it"
            )
        return Plan(
            self.name,
            params.model_dump(mode="json"),
            f"Unlock folder {params.path}",
            preconditions=(state,),
        )

    def apply(self, plan: Plan, fs: GuardedFS) -> Applied:
        p = UnlockParams.model_validate(plan.params)
        fs.set_lock(p.path, False)
        return Applied({"path": p.path}, post=(fs.state(p.path),))

    def inverse(self, plan: Plan, applied: Applied, view: ReadOnlyFS) -> Plan | None:
        path = str(applied.result["path"])
        return Plan(
            Lock.name,
            {"path": path},
            f"Lock folder {path} again",
            preconditions=(PathState(path, "dir"),),
        )

    def examples(self) -> list[dict[str, Any]]:
        return []  # needs a locked folder; covered by tests/integration/test_executor.py


BUILTIN_ACTIONS: tuple[type[_Action], ...] = (
    Mkdir,
    Mkdirs,
    Rmdir,
    Move,
    Rename,
    Delete,
    Create,
    Upload,
    Edit,
    Lock,
    Unlock,
)


def register_actions(r: Registry) -> None:
    for cls in BUILTIN_ACTIONS:
        r.action(cls())  # type: ignore[arg-type]
