# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The share action types: `shares.create`, `shares.update`, `shares.revoke`.

They change only the module's own rows (through `GuardedFS.write_state`), so they are
journaled like file changes and undone the same way. The rules of plan §14.4 are checked
while planning, so a share that breaks them never exists: no library roots, nothing
`ReadOnlyFS.privacy` says can't be shared (locked, hidden, `local-only`, `excluded`,
non-shareable folder types). Passwords and snapshot file lists travel in
`Plan.transient` and never reach the journal; tokens are made in `apply`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection

from steward.modules.shares import credentials, store
from steward.sdk import (
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

SHARE_ID = r"^[0-9a-f]{32}$"
Password = Field(None, min_length=credentials.MIN_PASSWORD, max_length=credentials.MAX_PASSWORD)


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError("needs a time zone")
    return value.astimezone(UTC) if value is not None else None


def _when(value: datetime | None) -> str:
    return "with no expiry" if value is None else f"until {value:%Y-%m-%d %H:%M} UTC"


class _Action:
    name: ClassVar[str]
    params: ClassVar[type[BaseModel]]
    ai_mode: ClassVar[AiMode] = "proposal"
    sensitive: ClassVar[bool] = False

    def render(self, plan: Plan) -> Rendered:
        return Rendered(plan.summary, tuple(plan.data.get("details", ())))

    def examples(self) -> list[dict[str, Any]]:
        return []


def _share(view: ReadOnlyFS, share_id: str) -> store.Share:
    with view.read_state() as conn:
        found = store.get(conn, share_id)
    if found is None:
        raise ActionRefused("no such share")
    return found


def _reads(view: ReadOnlyFS, path: str) -> tuple[str, ...]:
    """The share's item as a path the plan reads (so the journal hides the entry when
    the item is in a locked folder), unless its library is gone."""
    try:
        view.settings(path)
    except ActionRefused:
        return ()
    return (path,)


# --- create ------------------------------------------------------------------------------


class CreateParams(_Params):
    path: LibraryPath
    expires_at: datetime | None = None
    """None: the link never expires (sensitive)."""
    password: str | None = Password
    snapshot: bool = False
    """Folders only: share just the files there now, not ones added later."""
    share_id: str | None = Field(None, pattern=SHARE_ID)
    """Chosen by the caller to find the share afterwards (made up if not given)."""

    _aware = field_validator("expires_at")(_aware)


class Create(_Action):
    """Make a public link to a file or folder."""

    name = "shares.create"
    params = CreateParams

    def plan(self, params: BaseModel, view: ReadOnlyFS) -> Plan:
        assert isinstance(params, CreateParams)  # noqa: S101
        path = params.path
        if path.endswith(":"):
            raise ActionRefused("a whole library can't be shared; share a folder inside it")
        state = view.state(path)
        if state.kind == "absent":
            raise ActionRefused(f"nothing at {path}")
        privacy = view.privacy(path)
        if not privacy.shareable:
            raise ActionRefused(f"{path} can't be shared. {privacy.reason or ''}".strip())
        if params.expires_at is not None and params.expires_at <= view.now():
            raise ActionRefused("the expiry is in the past")
        if params.snapshot and state.kind != "dir":
            raise ActionRefused("only folders can be shared as a snapshot")
        files = view.file_ids_in(path) if params.snapshot else None
        share_id = params.share_id or credentials.new_share_id()
        summary = f"Share {path} {_when(params.expires_at)}, " + (
            "with a password" if params.password else "without a password"
        )
        details: list[str] = []
        if params.expires_at is None:
            details.append("The link never expires. Revoke it when it isn't needed any more.")
        if state.kind == "dir" and not params.snapshot:
            details.append("Files added to the folder later are shared too.")
        if files is not None:
            details.append(f"Only the {len(files)} file(s) there now are shared.")
        return Plan(
            self.name,
            {
                "path": path,
                "expires_at": params.expires_at.isoformat() if params.expires_at else None,
                "password": params.password is not None,
                "snapshot": params.snapshot,
                "share_id": share_id,
            },
            summary,
            preconditions=(PathState(path, state.kind),),
            reads=(path,),
            data={"kind": state.kind, "details": details},
            sensitive=params.expires_at is None,
            transient={"password": params.password, "files": files},
        )

    def apply(self, plan: Plan, fs: GuardedFS) -> Applied:
        p = plan.params
        password = plan.transient.get("password")
        if p["password"] and not password:
            raise ActionRefused("the share's password is missing; create the share again")
        files = plan.transient.get("files")
        if p["snapshot"] and files is None:
            raise ActionRefused("the snapshot's files are missing; create the share again")
        password_hash = credentials.hash_password(password) if password else None
        expires_at = datetime.fromisoformat(p["expires_at"]) if p["expires_at"] else None
        share_id, actor, now = str(p["share_id"]), fs.actor, fs.now()

        def write(conn: Connection) -> None:
            store.insert_share(
                conn,
                share_id=share_id,
                token=credentials.new_token(),
                path=str(p["path"]),
                kind=str(plan.data["kind"]),
                password_hash=password_hash,
                expires_at=expires_at,
                snapshot_files=files,
                actor=actor,
                now=now,
            )

        fs.write_state(write)
        return Applied({"share_id": share_id, "path": p["path"]})

    def inverse(self, plan: Plan, applied: Applied, view: ReadOnlyFS) -> Plan | None:
        path = str(applied.result["path"])
        return Plan(
            Revoke.name,
            {"share_id": applied.result["share_id"], "reason": "undone"},
            f"Turn off the link to {path}",
            reads=_reads(view, path),
        )


# --- update ------------------------------------------------------------------------------


class UpdateParams(_Params):
    share_id: str = Field(pattern=SHARE_ID)
    expiry: Literal["keep", "set", "never"] = "keep"
    expires_at: datetime | None = None
    """With `expiry: set`."""
    password_change: Literal["keep", "set", "remove"] = "keep"
    new_password: str | None = Password
    """With `password_change: set`."""

    _aware = field_validator("expires_at")(_aware)


class Update(_Action):
    """Change a share's expiry or password."""

    name = "shares.update"
    params = UpdateParams

    def plan(self, params: BaseModel, view: ReadOnlyFS) -> Plan:
        assert isinstance(params, UpdateParams)  # noqa: S101
        share = _share(view, params.share_id)
        now = view.now()
        if not share.active(now):
            raise ActionRefused("the share has ended; make a new one")
        if params.expiry == "set" and (params.expires_at is None or params.expires_at <= now):
            raise ActionRefused("choose an expiry in the future")
        if params.password_change == "set" and not params.new_password:
            raise ActionRefused("enter the new password")
        changes: list[str] = []
        new_expiry = share.expires_at
        if params.expiry == "set" and params.expires_at != share.expires_at:
            new_expiry = params.expires_at
            changes.append(f"expire {_when(new_expiry).removeprefix('until ')}")
        elif params.expiry == "never" and share.expires_at is not None:
            new_expiry = None
            changes.append("never expire")
        change = "keep"
        if params.password_change == "set":
            change = "set"
            changes.append("use a new password")
        elif params.password_change == "remove" and share.has_password:
            change = "remove"
            changes.append("need no password")
        if not changes:
            raise ActionRefused("nothing to change")
        sensitive = new_expiry is None and share.expires_at is not None
        expiry = (
            "keep" if new_expiry == share.expires_at else "never" if new_expiry is None else "set"
        )
        return Plan(
            self.name,
            {
                "share_id": share.share_id,
                "expiry": expiry,
                "expires_at": new_expiry.isoformat() if new_expiry else None,
                "password_change": change,
            },
            f"Change the link to {share.path}: " + ", ".join(changes),
            reads=_reads(view, share.path),
            data={
                "path": share.path,
                "old_expires_at": share.expires_at.isoformat() if share.expires_at else None,
                "details": (
                    ["The link will never expire. Revoke it when it isn't needed any more."]
                    if sensitive
                    else []
                ),
            },
            sensitive=sensitive,
            transient={"new_password": params.new_password},
        )

    def apply(self, plan: Plan, fs: GuardedFS) -> Applied:
        p = plan.params
        values: dict[str, Any] = {}
        if p["expiry"] != "keep":
            values["expires_at"] = (
                datetime.fromisoformat(p["expires_at"]) if p["expires_at"] else None
            )
        if p["password_change"] == "set":
            secret = plan.transient.get("new_password")
            if not secret:
                raise ActionRefused("the new password is missing; change the share again")
            values["password_hash"] = credentials.hash_password(secret)
        elif p["password_change"] == "remove":
            values["password_hash"] = None
        share_id, now = str(p["share_id"]), fs.now()

        def write(conn: Connection) -> None:
            if not store.change(conn, share_id, now=now, **values):
                raise ActionRefused("the share has ended; make a new one")

        fs.write_state(write)
        return Applied({"share_id": share_id})

    def inverse(self, plan: Plan, applied: Applied, view: ReadOnlyFS) -> Plan | None:
        p = plan.params
        if p["password_change"] != "keep" or p["expiry"] == "keep":
            return None  # an old password can't be restored (it was never stored)
        old = plan.data["old_expires_at"]
        return Plan(
            self.name,
            {
                "share_id": p["share_id"],
                "expiry": "set" if old else "never",
                "expires_at": old,
                "password_change": "keep",
            },
            f"Change the link to {plan.data['path']} back to "
            + _when(datetime.fromisoformat(old) if old else None),
            reads=plan.reads,
            data={"path": plan.data["path"], "old_expires_at": p["expires_at"]},
            sensitive=old is None,
        )


# --- revoke ------------------------------------------------------------------------------


class RevokeParams(_Params):
    share_id: str = Field(pattern=SHARE_ID)
    reason: Literal["revoked", "undone"] = "revoked"


class Revoke(_Action):
    """Turn a link off for good (it moves to the past shares; make a new one to share
    again)."""

    name = "shares.revoke"
    params = RevokeParams
    ai_mode = "auto"  # reduces exposure

    def plan(self, params: BaseModel, view: ReadOnlyFS) -> Plan:
        assert isinstance(params, RevokeParams)  # noqa: S101
        share = _share(view, params.share_id)
        if not share.active(view.now()):
            raise ActionRefused("the share has already ended")
        return Plan(
            self.name,
            params.model_dump(mode="json"),
            f"Turn off the link to {share.path}",
            reads=_reads(view, share.path),
        )

    def apply(self, plan: Plan, fs: GuardedFS) -> Applied:
        p = RevokeParams.model_validate(plan.params)
        actor, now = fs.actor, fs.now()

        def write(conn: Connection) -> None:
            if not store.end(conn, p.share_id, reason=p.reason, actor=actor, now=now):
                raise ActionRefused("the share has already ended")

        fs.write_state(write)
        return Applied({"share_id": p.share_id})

    def inverse(self, plan: Plan, applied: Applied, view: ReadOnlyFS) -> Plan | None:
        return None  # a revoked link stays off


ACTIONS = (Create(), Update(), Revoke())


def register_actions(r: Registry) -> None:
    for action in ACTIONS:
        r.action(action)  # type: ignore[arg-type]
