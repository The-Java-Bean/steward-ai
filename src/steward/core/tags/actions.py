# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The tag action types (plan §13.2, §13.8): `tag.add` and `tag.remove` record your
decision about one tag of one file; `tag.create`, `tag.update` (rename, description,
status) and `tag.merge` change the vocabulary. All change only rows (through
`GuardedFS.write_state`), are journaled and can be undone. An undo plan uses the same
action type with the result to revert in `data["revert"]`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection

from steward.core.tags import store
from steward.core.tags.names import TagError, normalize
from steward.sdk import (
    ActionRefused,
    AiMode,
    Applied,
    GuardedFS,
    LibraryPath,
    Plan,
    ReadOnlyFS,
    Registry,
    Rendered,
)

TagName = Field(min_length=1, max_length=300)


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _norm(name: str) -> str:
    try:
        return normalize(name)
    except TagError as exc:
        raise ActionRefused(str(exc)) from None


def _read[T](view: ReadOnlyFS, fn: Callable[[Connection], T]) -> T:
    with view.read_state() as conn:
        try:
            return fn(conn)
        except TagError as exc:
            raise ActionRefused(str(exc)) from None


def _write(fs: GuardedFS, op: Callable[[Connection], None]) -> None:
    def guarded(conn: Connection) -> None:
        try:
            op(conn)
        except TagError as exc:
            raise ActionRefused(str(exc)) from None

    fs.write_state(guarded)


class _Action:
    name: ClassVar[str]
    params: ClassVar[type[BaseModel]]
    ai_mode: ClassVar[AiMode] = "proposal"
    sensitive: ClassVar[bool] = False

    def render(self, plan: Plan) -> Rendered:
        return Rendered(plan.summary)

    def examples(self) -> list[dict[str, Any]]:
        return []


# --- a file's tags -----------------------------------------------------------------------


class FileTagParams(_Params):
    path: LibraryPath
    tag: str = TagName


class _FileTag(_Action):
    target: ClassVar[Literal["added", "removed"]]

    def plan(self, params: BaseModel, view: ReadOnlyFS) -> Plan:
        assert isinstance(params, FileTagParams)  # noqa: S101
        state = view.state(params.path)
        if state.kind != "file":
            raise ActionRefused(f"{params.path} isn't a file")
        file_id = view.file_id(params.path)
        if file_id is None:
            raise ActionRefused(f"{params.path} hasn't been scanned yet; try again shortly")
        name = _norm(params.tag)

        def look(conn: Connection) -> tuple[store.Tag | None, str | None]:
            tag = store.resolve(conn, name)
            return tag, store.file_state(conn, file_id, tag.tag_id) if tag else None

        tag, previous = _read(view, look)
        if tag is None and self.target == "removed":
            raise ActionRefused(f"there's no tag {name!r}")
        shown = tag.name if tag is not None else name
        if previous == self.target:
            raise ActionRefused(
                f"{params.path} already has the tag {shown!r}"
                if self.target == "added"
                else f"the tag {shown!r} was already removed from {params.path}"
            )
        summary = (
            f"Tag {params.path} with {shown}"
            if self.target == "added"
            else f"Remove the tag {shown} from {params.path}"
        )
        return Plan(
            self.name,
            {"path": params.path, "tag": shown},
            summary,
            preconditions=(state,),
            reads=(params.path,),
            data={"file_id": file_id, "state": self.target},
        )

    def apply(self, plan: Plan, fs: GuardedFS) -> Applied:
        data, name = plan.data, str(plan.params["tag"])
        file_id, actor, now = str(data["file_id"]), fs.actor, fs.now()

        def look(conn: Connection) -> tuple[store.Tag | None, str | None]:
            tag = store.resolve(conn, name)
            return tag, store.file_state(conn, file_id, tag.tag_id) if tag else None

        tag, previous = _read(fs, look)
        if tag is None and (data.get("undo") or self.target == "removed"):
            raise ActionRefused(f"there's no tag {name!r} any more")
        created = tag is None

        def op(conn: Connection) -> None:
            found = tag or store.create(conn, name, actor=actor, now=now)
            store.set_file(conn, file_id, found.tag_id, data["state"], actor=actor, now=now)
            if data.get("delete_tag"):
                store.delete_unused(conn, found.tag_id, actor=actor)

        _write(fs, op)
        return Applied(
            {
                "path": plan.params["path"],
                "file_id": file_id,
                "tag": tag.name if tag else name,
                "previous": previous,
                "created": created,
            }
        )

    def inverse(self, plan: Plan, applied: Applied, view: ReadOnlyFS) -> Plan | None:
        if plan.data.get("undo"):
            return None
        r = applied.result
        path, name = str(r["path"]), str(r["tag"])
        return Plan(
            self.name,
            {"path": path, "tag": name},
            f"Undo: {'tag' if self.target == 'added' else 'untag'} {path} ({name})",
            reads=(path,),
            data={
                "file_id": r["file_id"],
                "state": r["previous"],
                "delete_tag": bool(r["created"]),
                "undo": True,
            },
        )


class AddTag(_FileTag):
    """Give a file a tag (a new name makes a new tag)."""

    name = "tag.add"
    params = FileTagParams
    ai_mode = "auto"  # additive
    target = "added"

    def examples(self) -> list[dict[str, Any]]:
        return [{"path": "lib:docs/a.txt", "tag": "letters/personal"}]


class RemoveTag(_FileTag):
    """Take a tag off a file (also one the AI or the folder type gave it)."""

    name = "tag.remove"
    params = FileTagParams
    target = "removed"

    def examples(self) -> list[dict[str, Any]]:
        return []  # needs an existing tag; covered by tests/integration/test_tags.py


# --- the vocabulary ----------------------------------------------------------------------


class CreateParams(_Params):
    name: str = TagName
    description: str = Field("", max_length=store.MAX_DESCRIPTION)
    status: Literal["active", "ai-created"] = "active"


class CreateTag(_Action):
    """Add a tag to the vocabulary."""

    name = "tag.create"
    params = CreateParams
    ai_mode = "auto"  # an AI's tags are `ai-created`, for review

    def plan(self, params: BaseModel, view: ReadOnlyFS) -> Plan:
        assert isinstance(params, CreateParams)  # noqa: S101
        name = _norm(params.name)
        if _read(view, lambda conn: store.find(conn, name)) is not None:
            raise ActionRefused(f"{name!r} already exists")
        return Plan(
            self.name,
            {**params.model_dump(mode="json"), "name": name},
            f"Create the tag {name}",
        )

    def apply(self, plan: Plan, fs: GuardedFS) -> Applied:
        p, actor, now = plan.params, fs.actor, fs.now()
        name = str(p["name"])
        if plan.data.get("revert"):
            found = _read(fs, lambda conn: store.find(conn, name))
            if found is None or found.is_alias:
                raise ActionRefused(f"there's no tag {name!r} any more")

            def remove(conn: Connection) -> None:
                store.delete_unused(conn, found.tag_id, actor=actor)

            _write(fs, remove)
            return Applied({"name": name})

        def op(conn: Connection) -> None:
            store.create(
                conn,
                name,
                status=p["status"],
                description=str(p["description"]),
                actor=actor,
                now=now,
            )

        _write(fs, op)
        return Applied({"name": name})

    def inverse(self, plan: Plan, applied: Applied, view: ReadOnlyFS) -> Plan | None:
        if plan.data.get("revert"):
            return None
        return Plan(
            self.name,
            plan.params,
            f"Delete the tag {applied.result['name']}",
            data={"revert": True},
        )


class UpdateParams(_Params):
    tag: str = TagName
    new_name: str | None = Field(None, min_length=1, max_length=300)
    """Rename it (the tags under it move along; old names keep working)."""
    description: str | None = Field(None, max_length=store.MAX_DESCRIPTION)
    status: Literal["active", "deprecated"] | None = None
    """`active` also accepts an `ai-created` tag."""


class UpdateTag(_Action):
    """Rename a tag, change its description, deprecate it or make it active again."""

    name = "tag.update"
    params = UpdateParams

    def plan(self, params: BaseModel, view: ReadOnlyFS) -> Plan:
        assert isinstance(params, UpdateParams)  # noqa: S101
        tag = _read(view, lambda conn: store.require(conn, params.tag))
        new_name = _norm(params.new_name) if params.new_name is not None else None
        if new_name == tag.name:
            new_name = None
        changes = []
        if new_name is not None:
            changes.append(f"rename it to {new_name}")
        if params.description is not None and params.description.strip() != tag.description:
            changes.append("change its description")
        if params.status is not None and params.status != tag.status:
            changes.append(
                "deprecate it"
                if params.status == "deprecated"
                else "accept it"
                if tag.status == "ai-created"
                else "make it active again"
            )
        if not changes:
            raise ActionRefused("nothing to change")
        return Plan(
            self.name,
            {
                "tag": tag.name,
                "new_name": new_name,
                "description": params.description,
                "status": params.status,
            },
            f"Tag {tag.name}: " + ", ".join(changes),
            data={"tag_id": tag.tag_id},
        )

    def apply(self, plan: Plan, fs: GuardedFS) -> Applied:
        p, actor, now = plan.params, fs.actor, fs.now()
        tag_id = int(plan.data["tag_id"])
        tag = _read(fs, lambda conn: store.get(conn, tag_id))
        if tag is None or tag.is_alias:
            raise ActionRefused("the tag doesn't exist any more")
        revert = plan.data.get("revert")

        def op(conn: Connection) -> None:
            if revert is not None:
                if revert["name"] != tag.name:
                    store.rename(conn, tag_id, revert["name"], keep_alias=False, actor=actor)
                store.update_tag(
                    conn,
                    tag_id,
                    status=revert["status"],
                    description=revert["description"],
                    actor=actor,
                    now=now,
                )
                return
            if p["new_name"]:
                store.rename(conn, tag_id, str(p["new_name"]), actor=actor, now=now)
            store.update_tag(
                conn,
                tag_id,
                status=p["status"],
                description=p["description"],
                actor=actor,
                now=now,
            )

        _write(fs, op)
        return Applied({"name": tag.name, "status": tag.status, "description": tag.description})

    def inverse(self, plan: Plan, applied: Applied, view: ReadOnlyFS) -> Plan | None:
        if "revert" in plan.data:
            return None
        name = plan.params["new_name"] or plan.params["tag"]
        return Plan(
            self.name,
            plan.params,
            f"Undo the change to the tag {name}",
            data={"tag_id": plan.data["tag_id"], "revert": dict(applied.result)},
        )


class MergeParams(_Params):
    tag: str = TagName
    into: str = TagName


class MergeTag(_Action):
    """Merge a tag into another: files with it get the other one, and its name becomes
    an alias of the other."""

    name = "tag.merge"
    params = MergeParams

    def plan(self, params: BaseModel, view: ReadOnlyFS) -> Plan:
        assert isinstance(params, MergeParams)  # noqa: S101

        def look(conn: Connection) -> tuple[store.Tag, store.Tag]:
            return store.require(conn, params.tag), store.require(conn, params.into)

        tag, into = _read(view, look)
        if tag.tag_id == into.tag_id:
            raise ActionRefused("a tag can't be merged into itself")
        return Plan(
            self.name,
            {"tag": tag.name, "into": into.name},
            f"Merge the tag {tag.name} into {into.name}",
            data={"tag_id": tag.tag_id, "into_id": into.tag_id},
        )

    def apply(self, plan: Plan, fs: GuardedFS) -> Applied:
        actor, now = fs.actor, fs.now()
        revert = plan.data.get("revert")
        if revert is not None:

            def separate(conn: Connection) -> None:
                store.unmerge(conn, revert["merged"], actor=actor, now=now)

            _write(fs, separate)
            return Applied({"tag": plan.params["tag"], "into": plan.params["into"]})
        merged = _read(
            fs,
            lambda conn: store.prepare_merge(
                conn, int(plan.data["tag_id"]), int(plan.data["into_id"])
            ),
        )

        def op(conn: Connection) -> None:
            store.merge(conn, merged, actor=actor, now=now)

        _write(fs, op)
        return Applied({"tag": plan.params["tag"], "into": plan.params["into"], "merged": merged})

    def inverse(self, plan: Plan, applied: Applied, view: ReadOnlyFS) -> Plan | None:
        if "revert" in plan.data:
            return None
        return Plan(
            self.name,
            plan.params,
            f"Separate the tag {plan.params['tag']} from {plan.params['into']} again",
            data={**plan.data, "revert": dict(applied.result)},
        )


TAG_ACTIONS: tuple[_Action, ...] = (AddTag(), RemoveTag(), CreateTag(), UpdateTag(), MergeTag())


def register_tag_actions(r: Registry) -> None:
    for action in TAG_ACTIONS:
        r.action(action)  # type: ignore[arg-type]
