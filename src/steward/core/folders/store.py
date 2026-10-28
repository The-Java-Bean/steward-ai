# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Reading and changing libraries, folder types, folder identities, mappings and roles.

Every function takes a connection from `Database.write()` (or `read()` for loads), and
every change emits its event in the same transaction. `actor` says who made the change
(`user:<id>`, `system`, `setup`, ...).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from sqlalchemy import ColumnElement, Connection, Table, delete, func, literal, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError

from steward.core import events
from steward.core.db.schema import (
    files,
    folder_mappings,
    folder_roles,
    folder_types,
    folders,
    libraries,
    path_history,
)
from steward.core.db.types import utcnow
from steward.core.folders.paths import FolderRef, PathError, check_library_id, is_within
from steward.core.folders.policy import (
    ROLES,
    FolderMapping,
    Library,
    Policy,
    Role,
)
from steward.core.folders.types import (
    BUILTIN_TYPES,
    FolderTypeError,
    TypeDefinition,
    TypeSettings,
    check_type_name,
    resolve_types,
)

STEWARD_DIR = ".steward"
DEFAULT_ROLE_PATHS: dict[Role, tuple[str, str]] = {
    # role: (relative path inside the first library, folder type)
    "trash": (f"{STEWARD_DIR}/trash", "trash"),
    "edit_backups": (f"{STEWARD_DIR}/backups", "archive"),
}


class FolderError(Exception):
    """A folder operation isn't allowed (collision, permanent lock, type in use...)."""


class PermanentLockError(FolderError):
    """The folder is permanently locked; only `steward folders unlock --permanent` may unlock it."""


# --- loading ----------------------------------------------------------------------


def load_policy(conn: Connection) -> Policy:
    libs = {
        r.library_id: Library(r.library_id, r.name, r.root, r.position)
        for r in conn.execute(select(libraries).order_by(libraries.c.position))
    }
    overrides: dict[str, TypeSettings] = {}
    custom: dict[str, TypeDefinition] = {}
    for r in conn.execute(select(folder_types)):
        if r.builtin:
            if r.name in BUILTIN_TYPES and r.settings:
                overrides[r.name] = TypeSettings.model_validate(r.settings)
        else:
            custom[r.name] = TypeDefinition.model_validate({**r.settings, "extends": r.extends})
    rows = conn.execute(
        select(folder_mappings, folders.c.library_id, folders.c.relpath).join(
            folders, folders.c.folder_id == folder_mappings.c.folder_id
        )
    )
    mappings = {}
    for r in rows:
        ref = FolderRef(r.library_id, r.relpath)
        mappings[ref] = FolderMapping(r.folder_id, ref, r.type_name, r.locked, bool(r.permanent))
    role_rows = conn.execute(
        select(folder_roles.c.role, folders.c.library_id, folders.c.relpath).join(
            folders, folders.c.folder_id == folder_roles.c.folder_id
        )
    )
    roles = {r.role: FolderRef(r.library_id, r.relpath) for r in role_rows}
    return Policy(libs, resolve_types(overrides, custom), mappings, roles)


# --- libraries --------------------------------------------------------------------


def upsert_library(
    conn: Connection,
    library_id: str,
    *,
    root: str,
    name: str | None = None,
    actor: str,
    now: datetime | None = None,
) -> bool:
    """Add a library or update its root and name. Returns True if it was added."""
    check_library_id(library_id)
    if not root.startswith("/"):
        raise PathError(f"library {library_id!r}: root {root!r} must be an absolute path")
    root = root.rstrip("/") or "/"
    now = now or utcnow()
    existing = conn.execute(
        select(libraries).where(libraries.c.library_id == library_id)
    ).one_or_none()
    if existing is None:
        position = conn.execute(
            select(func.coalesce(func.max(libraries.c.position), -1) + 1)
        ).scalar_one()
        conn.execute(
            libraries.insert().values(
                library_id=library_id,
                name=name or library_id,
                root=root,
                position=position,
                actor=actor,
                created_at=now,
                updated_at=now,
            )
        )
        ensure_folder(conn, FolderRef(library_id, ""), now=now)
        events.emit(
            conn, "library.added", {"library_id": library_id, "root": root}, actor=actor, now=now
        )
        return True
    new_name = name or existing.name
    if (existing.root, existing.name) != (root, new_name):
        conn.execute(
            update(libraries)
            .where(libraries.c.library_id == library_id)
            .values(root=root, name=new_name, actor=actor, updated_at=now)
        )
        events.emit(
            conn,
            "library.updated",
            {"library_id": library_id, "root": root, "old_root": existing.root},
            actor=actor,
            now=now,
        )
    return False


# --- folder identities ------------------------------------------------------------


def folder_id(conn: Connection, ref: FolderRef) -> int | None:
    return conn.execute(
        select(folders.c.folder_id).where(
            folders.c.library_id == ref.library_id, folders.c.relpath == ref.relpath
        )
    ).scalar_one_or_none()


def ensure_folder(conn: Connection, ref: FolderRef, *, now: datetime | None = None) -> int:
    """The folder's identity, creating it if needed. Doesn't touch the disk."""
    existing = folder_id(conn, ref)
    if existing is not None:
        return existing
    now = now or utcnow()
    try:
        result = conn.execute(
            folders.insert().values(
                library_id=ref.library_id, relpath=ref.relpath, created_at=now, updated_at=now
            )
        )
    except IntegrityError as exc:
        raise PathError(f"unknown library {ref.library_id!r}") from exc
    return int(result.inserted_primary_key[0])  # type: ignore[index]


def move_folder(
    conn: Connection, old: FolderRef, new_relpath: str, *, actor: str, now: datetime | None = None
) -> int:
    """Record that a folder was renamed or moved within its library. Its identity, and
    with it its mapping, lock and roles, follow it, as do all folders and files inside
    it. Returns how many folder identities moved."""
    new = FolderRef.of(old.library_id, new_relpath)
    if old.relpath == "":
        raise FolderError("a library root can't be moved")
    if is_within(new.relpath, old.relpath):
        raise FolderError(f"can't move {old} into itself ({new})")
    now = now or utcnow()
    if folder_id(conn, old) is None:
        ensure_folder(conn, old, now=now)

    # Identities already recorded at the destination must be leftovers of folders that
    # are gone; a folder that's present there is a real collision.
    at_destination = _subtree(conn, new)
    files_at_destination = _subtree(conn, new, table=files)
    if any(r.present for r in [*at_destination, *files_at_destination]):
        raise FolderError(f"{new} already exists")
    if at_destination:
        conn.execute(
            delete(folders).where(folders.c.folder_id.in_([r.folder_id for r in at_destination]))
        )
    if files_at_destination:
        conn.execute(
            delete(files).where(files.c.file_id.in_([r.file_id for r in files_at_destination]))
        )
    conn.execute(
        update(files)
        .where(*_under(files, old))
        .values(
            relpath=literal(new.relpath) + func.substr(files.c.relpath, len(old.relpath) + 1),
            updated_at=now,
        )
    )

    moved = _subtree(conn, old)
    for r in moved:
        relpath = new.relpath + r.relpath[len(old.relpath) :]
        conn.execute(
            update(folders)
            .where(folders.c.folder_id == r.folder_id)
            .values(relpath=relpath, updated_at=now)
        )
        conn.execute(
            path_history.insert().values(
                folder_id=r.folder_id,
                old_library_id=old.library_id,
                old_relpath=r.relpath,
                new_library_id=old.library_id,
                new_relpath=relpath,
                actor=actor,
                changed_at=now,
            )
        )
    events.emit(
        conn,
        "folder.moved",
        {"library_id": old.library_id, "old_relpath": old.relpath, "new_relpath": new.relpath},
        actor=actor,
        now=now,
    )
    return len(moved)


def _under(table: Table, ref: FolderRef) -> tuple[ColumnElement[bool], ...]:
    """WHERE clauses for rows of `table` at `ref` or inside it."""
    if ref.relpath == "":
        return (table.c.library_id == ref.library_id,)
    prefix = ref.relpath + "/"
    return (
        table.c.library_id == ref.library_id,
        (table.c.relpath == ref.relpath) | (func.substr(table.c.relpath, 1, len(prefix)) == prefix),
    )


def _subtree(conn: Connection, ref: FolderRef, *, table: Table = folders) -> list[Any]:
    key = table.c.folder_id if table is folders else table.c.file_id
    return list(
        conn.execute(select(key, table.c.relpath, table.c.present).where(*_under(table, ref)))
    )


# --- folder types -----------------------------------------------------------------


def define_type(
    conn: Connection,
    name: str,
    definition: TypeDefinition,
    *,
    actor: str,
    now: datetime | None = None,
) -> Literal["created", "updated", "unchanged"]:
    """Create or replace a custom type, or replace the owner's overrides of a built-in
    type."""
    check_type_name(name)
    builtin = name in BUILTIN_TYPES
    if builtin and definition.extends is not None:
        raise FolderTypeError(f"built-in type {name!r} can't extend another type")
    settings = definition.model_dump(mode="json", exclude_none=True, exclude={"extends"})
    _check_resolves(conn, name, definition)
    now = now or utcnow()
    existing = conn.execute(
        select(folder_types.c.extends, folder_types.c.settings).where(folder_types.c.name == name)
    ).one_or_none()
    if existing is None:
        conn.execute(
            folder_types.insert().values(
                name=name,
                builtin=False,
                extends=definition.extends,
                settings=settings,
                actor=actor,
                created_at=now,
                updated_at=now,
            )
        )
        events.emit(conn, "folder_type.created", {"name": name}, actor=actor, now=now)
        return "created"
    if (existing.extends, existing.settings) == (definition.extends, settings):
        return "unchanged"
    conn.execute(
        update(folder_types)
        .where(folder_types.c.name == name)
        .values(extends=definition.extends, settings=settings, actor=actor, updated_at=now)
    )
    events.emit(conn, "folder_type.updated", {"name": name}, actor=actor, now=now)
    return "updated"


def rename_type(
    conn: Connection, old: str, new: str, *, actor: str, now: datetime | None = None
) -> None:
    """Rename a custom type; mappings and types that extend it follow."""
    check_type_name(new)
    if old in BUILTIN_TYPES:
        raise FolderTypeError(f"built-in type {old!r} can't be renamed")
    if new in BUILTIN_TYPES or _type_exists(conn, new):
        raise FolderTypeError(f"a folder type named {new!r} already exists")
    if not _type_exists(conn, old):
        raise FolderTypeError(f"unknown folder type {old!r}")
    now = now or utcnow()
    conn.execute(
        update(folder_types)
        .where(folder_types.c.name == old)
        .values(name=new, actor=actor, updated_at=now)
    )
    events.emit(
        conn, "folder_type.renamed", {"old_name": old, "new_name": new}, actor=actor, now=now
    )


def delete_type(conn: Connection, name: str, *, actor: str, now: datetime | None = None) -> None:
    """Delete a custom type that no folder uses and no type extends."""
    if name in BUILTIN_TYPES:
        raise FolderTypeError(f"built-in type {name!r} can't be deleted")
    if not _type_exists(conn, name):
        raise FolderTypeError(f"unknown folder type {name!r}")
    try:
        with conn.begin_nested():
            conn.execute(delete(folder_types).where(folder_types.c.name == name))
    except IntegrityError as exc:
        raise FolderTypeError(
            f"folder type {name!r} is still used by a folder or extended by another type"
        ) from exc
    events.emit(conn, "folder_type.deleted", {"name": name}, actor=actor, now=now)


def _type_exists(conn: Connection, name: str) -> bool:
    return (
        conn.execute(select(folder_types.c.name).where(folder_types.c.name == name)).one_or_none()
        is not None
    )


def _check_resolves(conn: Connection, name: str, definition: TypeDefinition) -> None:
    """Raise FolderTypeError if the types wouldn't resolve with this change."""
    overrides: dict[str, TypeSettings] = {}
    custom: dict[str, TypeDefinition] = {}
    for r in conn.execute(select(folder_types)):
        if r.builtin:
            overrides[r.name] = TypeSettings.model_validate(r.settings)
        else:
            custom[r.name] = TypeDefinition.model_validate({**r.settings, "extends": r.extends})
    if name in BUILTIN_TYPES:
        overrides[name] = TypeSettings(**definition.model_dump(exclude={"extends"}))
    else:
        custom[name] = definition
    resolve_types(overrides, custom)


# --- mappings and locks -----------------------------------------------------------


def set_type(
    conn: Connection,
    ref: FolderRef,
    type_name: str | None,
    *,
    actor: str,
    now: datetime | None = None,
) -> None:
    """Map a folder to a type (None removes the folder's own type; it then inherits)."""
    if type_name is not None and not _type_exists(conn, type_name):
        raise FolderTypeError(f"unknown folder type {type_name!r}")
    now = now or utcnow()
    fid = ensure_folder(conn, ref, now=now)
    row = _mapping_row(conn, fid)
    old_type = row.type_name if row else None
    if old_type == type_name:
        return
    _write_mapping(
        conn,
        fid,
        type_name=type_name,
        locked=row.locked if row else None,
        permanent=bool(row.permanent) if row else False,
        actor=actor,
        now=now,
    )
    events.emit(
        conn,
        "folder.type_changed",
        {**_payload(fid, ref), "old_type": old_type, "new_type": type_name},
        actor=actor,
        now=now,
    )


def lock(
    conn: Connection,
    ref: FolderRef,
    *,
    permanent: bool = False,
    actor: str,
    now: datetime | None = None,
) -> None:
    """Lock a folder (and everything inside it). A permanent lock can only be removed
    with `steward folders unlock --permanent` on the host."""
    now = now or utcnow()
    fid = ensure_folder(conn, ref, now=now)
    row = _mapping_row(conn, fid)
    was_permanent = bool(row.permanent) if row else False
    if row is not None and row.locked and (was_permanent or not permanent):
        return  # already locked at least this firmly
    _write_mapping(
        conn,
        fid,
        type_name=row.type_name if row else None,
        locked=True,
        permanent=permanent or was_permanent,
        actor=actor,
        now=now,
    )
    events.emit(
        conn,
        "folder.locked",
        {**_payload(fid, ref), "permanent": permanent or was_permanent},
        actor=actor,
        now=now,
    )


def unlock(
    conn: Connection,
    ref: FolderRef,
    *,
    allow_permanent: bool = False,
    actor: str,
    now: datetime | None = None,
) -> None:
    """Unlock a folder, even if its type is a locked type. Refuses permanent locks
    unless `allow_permanent`, and locks that come from a parent folder."""
    now = now or utcnow()
    policy = load_policy(conn)
    settings = policy.settings_for(ref)
    if settings.locked and settings.locked_by != ref.relpath:
        parent = FolderRef(ref.library_id, settings.locked_by or "")
        raise FolderError(f"{ref} is locked because {parent} is; unlock that instead")
    fid = ensure_folder(conn, ref, now=now)
    row = _mapping_row(conn, fid)
    if row is not None and row.permanent and not allow_permanent:
        raise PermanentLockError(
            f"{ref} is permanently locked; use `steward folders unlock --permanent` on the host"
        )
    if not settings.locked:
        return
    _write_mapping(
        conn,
        fid,
        type_name=row.type_name if row else None,
        locked=False,
        permanent=False,
        actor=actor,
        now=now,
    )
    events.emit(conn, "folder.unlocked", _payload(fid, ref), actor=actor, now=now)


def clear_lock_override(
    conn: Connection, ref: FolderRef, *, actor: str, now: datetime | None = None
) -> None:
    """Drop the folder's own lock setting so it follows its type again. Refused for
    permanent locks."""
    fid = folder_id(conn, ref)
    row = _mapping_row(conn, fid) if fid is not None else None
    if fid is None or row is None or row.locked is None:
        return
    if row.permanent:
        raise PermanentLockError(f"{ref} is permanently locked")
    _write_mapping(
        conn,
        fid,
        type_name=row.type_name,
        locked=None,
        permanent=False,
        actor=actor,
        now=now or utcnow(),
    )


def _mapping_row(conn: Connection, fid: int) -> Any:
    return conn.execute(
        select(folder_mappings).where(folder_mappings.c.folder_id == fid)
    ).one_or_none()


def _write_mapping(
    conn: Connection,
    fid: int,
    *,
    type_name: str | None,
    locked: bool | None,
    permanent: bool,
    actor: str,
    now: datetime,
) -> None:
    if type_name is None and locked is None and not permanent:
        conn.execute(delete(folder_mappings).where(folder_mappings.c.folder_id == fid))
        return
    values = {
        "type_name": type_name,
        "locked": locked,
        "permanent": permanent,
        "actor": actor,
        "updated_at": now,
    }
    conn.execute(
        insert(folder_mappings)
        .values(folder_id=fid, **values)
        .on_conflict_do_update(index_elements=[folder_mappings.c.folder_id], set_=values)
    )


def _payload(fid: int, ref: FolderRef) -> dict[str, Any]:
    return {"folder_id": fid, "library_id": ref.library_id, "relpath": ref.relpath}


# --- roles ------------------------------------------------------------------------


def set_role(
    conn: Connection, role: Role, ref: FolderRef, *, actor: str, now: datetime | None = None
) -> None:
    if role not in ROLES:
        raise FolderError(f"unknown role {role!r}; roles are {', '.join(ROLES)}")
    now = now or utcnow()
    fid = ensure_folder(conn, ref, now=now)
    settings = load_policy(conn).settings_for(ref)
    if not settings.writable:
        raise FolderError(
            f"{ref} isn't writable ({settings.type.name}), so it can't hold the {role} role"
        )
    if settings.locked and role in ("inbox", "digest_output"):
        raise FolderError(f"{ref} is locked, so it can't hold the {role} role")
    current = conn.execute(
        select(folder_roles.c.folder_id).where(folder_roles.c.role == role)
    ).scalar_one_or_none()
    if current == fid:
        return
    values = {"folder_id": fid, "actor": actor, "updated_at": now}
    conn.execute(
        insert(folder_roles)
        .values(role=role, **values)
        .on_conflict_do_update(index_elements=[folder_roles.c.role], set_=values)
    )
    events.emit(
        conn, "folder.role_changed", {**_payload(fid, ref), "role": role}, actor=actor, now=now
    )


def clear_role(conn: Connection, role: Role, *, actor: str, now: datetime | None = None) -> None:
    result = conn.execute(delete(folder_roles).where(folder_roles.c.role == role))
    if result.rowcount:
        events.emit(
            conn, "folder.role_changed", {"role": role, "folder_id": None}, actor=actor, now=now
        )


def ensure_default_roles(
    conn: Connection, *, actor: str, now: datetime | None = None
) -> list[Role]:
    """Give `trash` and `edit_backups` hidden default folders inside the first library if
    the owner hasn't chosen any. Records identities and mappings only; the executor
    creates the directories when it first needs them. Returns the roles assigned."""
    policy = load_policy(conn)
    if not policy.libraries:
        return []
    first = min(policy.libraries.values(), key=lambda lib: lib.position)
    assigned: list[Role] = []
    for role, (relpath, type_name) in DEFAULT_ROLE_PATHS.items():
        if role in policy.roles:
            continue
        ref = FolderRef(first.library_id, relpath)
        if ref not in policy.mappings:
            set_type(conn, ref, type_name, actor=actor, now=now)
        set_role(conn, role, ref, actor=actor, now=now)
        assigned.append(role)
    return assigned
