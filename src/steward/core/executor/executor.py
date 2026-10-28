# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The executor: plans, checks, applies and journals actions; undoes them (plan §13.5).

`Executor.run(action, params, actor=..., confirmed=...)`, all under the process-wide
write lock (the API process is the only writer, docs/adr/0005):

1. Validate the parameters with the action type's model and make the plan (read-only).
2. Check every path the plan reads or writes. An action is **sensitive** if its type
   says so or it touches a locked folder; sensitive actions need `confirmed=True` (the
   caller checked the password recently) or they're refused.
3. Re-check the plan's preconditions, then `apply` through a `Guarded` handle that
   only allows writes at the plan's `writes` paths.
4. In one `state.db` transaction: folder setting changes, file/folder identities
   follow moves (so a moved file keeps its `file_id`, mapping and lock), the journal
   entry with its undo plan, the `action.applied` event, and `core.scan` jobs for the
   changed paths.

Undo runs the stored undo plan the same way; its preconditions are the state the
action left, so undo is refused once anything changed since.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import Connection, delete, select, update

from steward.core import events, queue
from steward.core.db import Databases, utcnow
from steward.core.db.schema import files, journal, path_history
from steward.core.discovery.watcher import SCAN_JOB
from steward.core.executor.dirs import WRITE_LOCK
from steward.core.executor.guarded import Guarded, ReadOnlyView, Target
from steward.core.folders import store as folder_store
from steward.core.folders.paths import FolderRef, PathError
from steward.core.folders.policy import Policy
from steward.core.folders.store import load_policy
from steward.sdk import ActionRefused, ActionType, Applied, Plan, Rendered

log = logging.getLogger(__name__)

SCAN_PRIORITY = 20
"""Rescans after an action run before the watcher's (10) and the reconciler's (0)."""


class SensitiveActionError(ActionRefused):
    """The action is sensitive and the caller didn't confirm a recent password check."""


class ActionFailed(Exception):
    """The action failed while applying (an OS error). The journal has no entry; files
    may be partly changed (an edit's backup copy, for example)."""


@dataclass(frozen=True, slots=True)
class Preview:
    action: ActionType
    plan: Plan
    rendered: Rendered
    sensitive: bool


@dataclass(frozen=True, slots=True)
class JournalEntry:
    entry_id: int
    action: str
    summary: str
    actor: str
    sensitive: bool
    applied_at: datetime
    params: Mapping[str, Any]
    plan: Mapping[str, Any]
    result: Mapping[str, Any]
    inverse: Mapping[str, Any] | None
    undo_of: int | None
    undone_by: int | None
    proposal_id: int | None

    @property
    def undoable(self) -> bool:
        return self.inverse is not None and self.undone_by is None

    @property
    def paths(self) -> set[str]:
        """Every path the action read, wrote or checked (`library:relpath`)."""
        plan = self.plan
        return {
            *plan.get("reads", []),
            *plan.get("writes", []),
            *(p["path"] for p in plan.get("preconditions", [])),
        }


class Executor:
    def __init__(
        self,
        dbs: Databases,
        actions: Mapping[str, ActionType],
        *,
        clock: Callable[[], datetime] = utcnow,
        max_attempts: int = 5,
    ) -> None:
        self.dbs = dbs
        self.actions = dict(actions)
        self.clock = clock
        self.max_attempts = max_attempts

    # --- looking -------------------------------------------------------------------

    def action_type(self, name: str) -> ActionType:
        found = self.actions.get(name)
        if found is None:
            raise ActionRefused(f"unknown action {name!r}")
        return found

    def preview(self, name: str, params: Mapping[str, Any]) -> Preview:
        """Plan without applying (for confirmation dialogs)."""
        action = self.action_type(name)
        policy = self._policy()
        view = ReadOnlyView(policy, self.dbs.state, self.clock)
        plan = action.plan(self._params(action, params), view)
        self._check_paths(view, plan)
        return Preview(action, plan, action.render(plan), self._sensitive(action, plan, view))

    # --- running -------------------------------------------------------------------

    def run(
        self,
        name: str,
        params: Mapping[str, Any],
        *,
        actor: str,
        confirmed: bool = False,
        proposal_id: int | None = None,
    ) -> JournalEntry:
        action = self.action_type(name)
        parsed = self._params(action, params)
        with WRITE_LOCK:
            view = ReadOnlyView(self._policy(), self.dbs.state, self.clock)
            plan = action.plan(parsed, view)
            return self._apply(
                action, plan, view, actor=actor, confirmed=confirmed, proposal_id=proposal_id
            )

    def undo(self, entry_id: int, *, actor: str, confirmed: bool = False) -> JournalEntry:
        with WRITE_LOCK:
            entry = self.get(entry_id)
            if entry is None:
                raise ActionRefused(f"no journal entry {entry_id}")
            if entry.undone_by is not None:
                raise ActionRefused(f"entry {entry_id} was already undone")
            if entry.inverse is None:
                raise ActionRefused(f"{entry.action} can't be undone")
            plan = Plan.from_dict(entry.inverse)
            action = self.action_type(plan.action)
            if entry.sensitive and not confirmed:
                raise SensitiveActionError("undoing a sensitive action needs your password")
            view = ReadOnlyView(self._policy(), self.dbs.state, self.clock)
            return self._apply(
                action, plan, view, actor=actor, confirmed=confirmed, undo_of=entry_id
            )

    def _apply(
        self,
        action: ActionType,
        plan: Plan,
        view: ReadOnlyView,
        *,
        actor: str,
        confirmed: bool,
        undo_of: int | None = None,
        proposal_id: int | None = None,
    ) -> JournalEntry:
        if plan.action != action.name:
            raise ActionRefused(f"plan is for {plan.action!r}, not {action.name!r}")
        self._check_paths(view, plan)
        sensitive = self._sensitive(action, plan, view)
        if sensitive and not confirmed:
            raise SensitiveActionError(f"{plan.summary}: this needs your password")
        for want in plan.preconditions:
            actual = view.state(want.path, content_hash=want.content_hash is not None)
            if not want.satisfied_by(actual):
                raise ActionRefused(
                    f"{want.path} changed since the action was planned "
                    f"(expected {want.kind}, found {actual.kind})"
                )

        fs = Guarded(view.policy, self.dbs.state, self.clock)
        fs.allowed_writes = set(plan.writes)
        try:
            applied = action.apply(plan, fs)
        except ActionRefused:
            self._failed(action.name, actor, "refused")
            raise
        except OSError as exc:
            self._failed(action.name, actor, type(exc).__name__)
            raise ActionFailed(f"{plan.summary} failed: {exc.strerror or exc}") from exc
        try:
            inverse = action.inverse(
                plan, applied, ReadOnlyView(view.policy, self.dbs.state, self.clock)
            )
        except ActionRefused:
            inverse = None
        except Exception:
            log.exception("inverse plan failed", extra={"fields": {"action": action.name}})
            inverse = None

        now = self.clock()
        with self.dbs.state.write() as conn:
            for op in fs.db_ops:
                op(conn)
            for source, destination in applied.moves:
                self._record_move(conn, view, source, destination, actor=actor, now=now)
            entry_id = conn.execute(
                journal.insert()
                .values(
                    action=action.name,
                    params=dict(plan.params),
                    plan=plan.as_dict(),
                    result={**applied.result, "post": [p.as_dict() for p in applied.post]},
                    inverse=inverse.as_dict() if inverse is not None else None,
                    summary=plan.summary,
                    sensitive=sensitive,
                    actor=actor,
                    proposal_id=proposal_id,
                    undo_of=undo_of,
                    applied_at=now,
                )
                .returning(journal.c.entry_id)
            ).scalar_one()
            if undo_of is not None:
                conn.execute(
                    update(journal).where(journal.c.entry_id == undo_of).values(undone_by=entry_id)
                )
            events.emit(
                conn,
                "action.applied",
                {
                    "entry_id": entry_id,
                    "action": action.name,
                    "summary": plan.summary,
                    "undo_of": undo_of,
                },
                actor=actor,
                now=now,
            )
            self._queue_scans(conn, view, applied, now)
        log.info(
            "action applied",
            extra={"fields": {"entry_id": entry_id, "action": action.name, "actor": actor}},
        )
        found = self.get(entry_id)
        assert found is not None  # noqa: S101 -- just inserted
        return found

    # --- checks --------------------------------------------------------------------

    def _params(self, action: ActionType, params: Mapping[str, Any]) -> BaseModel:
        try:
            return action.params.model_validate(dict(params))
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(map(str, e['loc'])) or 'params'}: {e['msg']}" for e in exc.errors()
            )
            raise ActionRefused(f"invalid parameters for {action.name}: {problems}") from None

    def _check_paths(self, view: ReadOnlyView, plan: Plan) -> None:
        for path in (*plan.reads, *(p.path for p in plan.preconditions)):
            view.target(path)
        for path in plan.writes:
            view.target(path, new=True)

    def _sensitive(self, action: ActionType, plan: Plan, view: ReadOnlyView) -> bool:
        if action.sensitive:
            return True
        paths = {*plan.reads, *plan.writes, *(p.path for p in plan.preconditions)}
        for path in paths:
            target = view.target(path)
            if view.policy_of(target, folder=False).locked:
                return True
            if target.relpath and view.policy_of(target, folder=True).locked:
                return True
        return False

    # --- bookkeeping ---------------------------------------------------------------

    def _failed(self, action: str, actor: str, error: str) -> None:
        with self.dbs.state.write() as conn:
            events.emit(conn, "action.failed", {"action": action, "error": error}, actor=actor)

    def _record_move(
        self,
        conn: Connection,
        view: ReadOnlyView,
        source: str,
        destination: str,
        *,
        actor: str,
        now: datetime,
    ) -> None:
        src, dst = view.target(source), view.target(destination, new=True)
        row = conn.execute(
            select(files).where(
                files.c.library_id == src.library.library_id, files.c.relpath == src.relpath
            )
        ).one_or_none()
        if row is not None:
            # Whatever row is recorded at the destination is stale: the move only
            # succeeded because nothing was there.
            conn.execute(
                delete(files).where(
                    files.c.library_id == dst.library.library_id,
                    files.c.relpath == dst.relpath,
                )
            )
            conn.execute(
                update(files)
                .where(files.c.file_id == row.file_id)
                .values(library_id=dst.library.library_id, relpath=dst.relpath, updated_at=now)
            )
            conn.execute(
                path_history.insert().values(
                    file_id=row.file_id,
                    old_library_id=src.library.library_id,
                    old_relpath=src.relpath,
                    new_library_id=dst.library.library_id,
                    new_relpath=dst.relpath,
                    actor=actor,
                    changed_at=now,
                )
            )
            events.emit(
                conn,
                "file.moved",
                {
                    "file_id": row.file_id,
                    "library_id": dst.library.library_id,
                    "relpath": dst.relpath,
                    "content_hash": row.content_hash,
                    "old_library_id": src.library.library_id,
                    "old_relpath": src.relpath,
                },
                actor=actor,
                now=now,
            )
            return
        if src.library.library_id == dst.library.library_id and _is_dir(view, dst):
            try:
                folder_store.move_folder(
                    conn,
                    FolderRef(src.library.library_id, src.relpath),
                    dst.relpath,
                    actor=actor,
                    now=now,
                )
            except (folder_store.FolderError, PathError) as exc:
                log.warning(
                    "folder identity didn't follow a move; the next scan will reconcile",
                    extra={"fields": {"source": source, "error": type(exc).__name__}},
                )

    def _queue_scans(
        self, conn: Connection, view: ReadOnlyView, applied: Applied, now: datetime
    ) -> None:
        by_library: dict[str, list[str]] = {}
        for path in dict.fromkeys([*applied.changed, *(p for move in applied.moves for p in move)]):
            target = view.target(path)
            by_library.setdefault(target.library.library_id, []).append(target.relpath)
        for library_id, paths in by_library.items():
            queue.enqueue(
                conn,
                SCAN_JOB,
                lane="io",
                payload={"library_id": library_id, "paths": paths},
                priority=SCAN_PRIORITY,
                max_attempts=self.max_attempts,
                now=now,
            )

    def _policy(self) -> Policy:
        with self.dbs.state.read() as conn:
            return load_policy(conn)

    # --- the journal ---------------------------------------------------------------

    def get(self, entry_id: int) -> JournalEntry | None:
        with self.dbs.state.read() as conn:
            row = conn.execute(select(journal).where(journal.c.entry_id == entry_id)).one_or_none()
        return _entry(row) if row is not None else None

    def entries(self, *, limit: int = 50, before: int | None = None) -> list[JournalEntry]:
        query = select(journal).order_by(journal.c.entry_id.desc()).limit(limit)
        if before is not None:
            query = query.where(journal.c.entry_id < before)
        with self.dbs.state.read() as conn:
            return [_entry(row) for row in conn.execute(query)]


def _is_dir(view: ReadOnlyView, target: Target) -> bool:
    try:
        return view.state(target.path).kind == "dir"
    except ActionRefused:
        return False


def _entry(row: Any) -> JournalEntry:
    return JournalEntry(
        entry_id=row.entry_id,
        action=row.action,
        summary=row.summary,
        actor=row.actor,
        sensitive=bool(row.sensitive),
        applied_at=row.applied_at,
        params=row.params,
        plan=row.plan,
        result=row.result,
        inverse=row.inverse,
        undo_of=row.undo_of,
        undone_by=row.undone_by,
        proposal_id=row.proposal_id,
    )
