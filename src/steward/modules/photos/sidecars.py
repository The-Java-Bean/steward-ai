# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""XMP sidecars: what a photo's sidecar should say, and getting it there (docs/adr/0026).

The job `photos.sidecars` runs on the `executor` lane (the API process, the only
writer). For each folder and batch of photos whose tags or description changed it copies
the sidecars to a private temp folder under fixed names, reads them with one exiftool
process, edits them with another, and hands the bytes to the `photos.sidecar_sync` action. All
of the slow part happens before the action takes the write lock.

Merging is three-way, against what steward-ai recorded last time (`Record`): keywords
newly wanted are added, keywords no longer wanted are removed only if steward-ai added
them, and whatever another program did to the sidecar otherwise stays, including
removing one of steward-ai's keywords. A description is written only over nothing or
over steward-ai's own.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, Column, Connection, MetaData, Table, Text, select
from sqlalchemy.dialects.sqlite import insert

from steward.sdk import (
    SIDECAR_SUFFIX,
    ActionRefused,
    ActionRunner,
    AiMode,
    Applied,
    FileTag,
    GuardedFS,
    JobContext,
    LibraryAccess,
    LibraryFile,
    LibraryPath,
    PermanentJobError,
    Plan,
    ReadOnlyFS,
    Rendered,
    RetryLater,
    StateAccess,
    UTCDateTime,
    is_sidecar,
    utcnow,
)

log = logging.getLogger(__name__)

ACTION = "photos.sidecar_sync"
CAPTION = "text:caption"
PAGE = 500
MAX_ITEMS = 500
MAX_CONTENT = 16 * 1024 * 1024

SUBJECT = "-XMP-dc:Subject"
HIERARCHY = "-XMP-lr:HierarchicalSubject"
DESCRIPTION = "-XMP-dc:Description"

EMPTY_XMP = (
    '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
    "</rdf:RDF>\n"
    "</x:xmpmeta>\n"
    '<?xpacket end="w"?>\n'
).encode()

_XMP_START = re.compile(rb"^\s*(?:<\?xpacket|<\?xml|<x:xmpmeta|<rdf:RDF)")
_FORBIDDEN = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)

metadata = MetaData()
table = Table(
    "photos_sidecars",
    metadata,
    Column("file_id", Text, primary_key=True),
    Column("path", Text, nullable=False),
    Column("data", JSON, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
)
runs = Table(
    "photos_sidecar_runs",
    metadata,
    Column("name", Text, primary_key=True),
    Column("revision", Text, nullable=False),
)
RUN = "last"


def looks_like_xmp(data: bytes) -> bool:
    """A file only reaches exiftool if it is plain XMP: exiftool picks a parser by what it
    finds, and the API process has the libraries read-write. No entities, no DOCTYPE."""
    return _XMP_START.match(data[:256]) is not None and _FORBIDDEN.search(data) is None


def _digest(text: str | None) -> str | None:
    return hashlib.sha256(text.encode()).hexdigest() if text else None


@dataclass(frozen=True, slots=True)
class Wanted:
    subject: tuple[str, ...] = ()
    hierarchy: tuple[str, ...] = ()
    description: str | None = None

    @property
    def empty(self) -> bool:
        return not (self.subject or self.hierarchy or self.description)


def wanted(tags: Iterable[FileTag], caption: str | None) -> Wanted:
    """`finance/receipt` is `finance` and `receipt` in `dc:subject` (so a flat keyword search
    finds either) and `finance|receipt` in `lr:hierarchicalSubject`."""
    subject: set[str] = set()
    hierarchy: set[str] = set()
    for tag in tags:
        parts = [part.replace("|", " ").strip() for part in tag.name.split("/")]
        subject.update(parts)
        hierarchy.add("|".join(parts))
    return Wanted(
        tuple(sorted(subject)), tuple(sorted(hierarchy)), " ".join((caption or "").split()) or None
    )


@dataclass(frozen=True, slots=True)
class Record:
    """What steward-ai knows about a sidecar. `subject`, `hierarchy` and `description` (a
    hash: `state.db` keeps no description text) are what it last wanted; the `owned_`
    fields are the ones it put there itself."""

    subject: tuple[str, ...] = ()
    hierarchy: tuple[str, ...] = ()
    owned_subject: tuple[str, ...] = ()
    owned_hierarchy: tuple[str, ...] = ()
    description: str | None = None
    owned_description: bool = False

    def matches(self, want: Wanted) -> bool:
        return (
            self.subject == want.subject
            and self.hierarchy == want.hierarchy
            and self.description == _digest(want.description)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": list(self.subject),
            "hierarchy": list(self.hierarchy),
            "owned_subject": list(self.owned_subject),
            "owned_hierarchy": list(self.owned_hierarchy),
            "description": self.description,
            "owned_description": self.owned_description,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Record:
        return cls(
            tuple(data.get("subject", ())),
            tuple(data.get("hierarchy", ())),
            tuple(data.get("owned_subject", ())),
            tuple(data.get("owned_hierarchy", ())),
            data.get("description"),
            bool(data.get("owned_description", False)),
        )


@dataclass(frozen=True, slots=True)
class Current:
    """What a sidecar says now."""

    subject: frozenset[str] = frozenset()
    hierarchy: frozenset[str] = frozenset()
    description: str | None = None


@dataclass(frozen=True, slots=True)
class Edit:
    add_subject: tuple[str, ...] = ()
    remove_subject: tuple[str, ...] = ()
    add_hierarchy: tuple[str, ...] = ()
    remove_hierarchy: tuple[str, ...] = ()
    set_description: bool = False
    description: str | None = None

    @property
    def empty(self) -> bool:
        return not (
            self.add_subject
            or self.remove_subject
            or self.add_hierarchy
            or self.remove_hierarchy
            or self.set_description
        )


def _keywords(
    want: Sequence[str], last: Sequence[str], owned: Sequence[str], current: frozenset[str]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """(to add, to remove, owned afterwards)."""
    add = tuple(k for k in want if k not in last)
    remove = tuple(k for k in last if k not in want and k in owned and k in current)
    keep = [k for k in owned if k in want and k in current]
    return add, remove, tuple(sorted({*keep, *(k for k in add if k not in current)}))


def merge(want: Wanted, record: Record | None, current: Current) -> tuple[Edit, Record]:
    """The change to make and the record to keep afterwards."""
    record = record or Record()
    add_s, remove_s, owned_s = _keywords(
        want.subject, record.subject, record.owned_subject, current.subject
    )
    add_h, remove_h, owned_h = _keywords(
        want.hierarchy, record.hierarchy, record.owned_hierarchy, current.hierarchy
    )
    ours = record.owned_description and _digest(current.description) == record.description
    free = current.description is None or ours
    change = free and (current.description or None) != want.description
    return (
        Edit(add_s, remove_s, add_h, remove_h, change, want.description),
        Record(
            want.subject,
            want.hierarchy,
            owned_s,
            owned_h,
            _digest(want.description),
            free and want.description is not None,
        ),
    )


# --- exiftool ---------------------------------------------------------------------------


class SidecarToolFailed(Exception):
    """exiftool gave no usable answer (no message: it could hold file content)."""


def _env() -> dict[str, str]:
    return {"LC_ALL": "C", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def _run(args: list[str], *, cwd: Path, timeout_s: float) -> bytes:
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed arguments, the program is configured
            args,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_env(),
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise SidecarToolFailed from None
    return proc.stdout


def _strings(value: Any) -> frozenset[str]:
    """exiftool answers one keyword as a scalar, several as a list."""
    if value is None or value == "":
        return frozenset()
    return frozenset(str(v) for v in (value if isinstance(value, list) else [value]))


def read_current(
    command: str, names: Sequence[str], *, cwd: Path, timeout_s: float
) -> dict[str, Current]:
    """What the sidecars `names` (in `cwd`) say; a file exiftool can't read is left out."""
    if not names:
        return {}
    args = [command, "-json", "-q", SUBJECT, HIERARCHY, DESCRIPTION, *names]
    out = _run(args, cwd=cwd, timeout_s=timeout_s)
    try:
        records = json.loads(out)
    except ValueError:
        raise SidecarToolFailed from None
    if not isinstance(records, list):
        raise SidecarToolFailed
    found = {}
    for record in records:
        if not isinstance(record, dict) or "Error" in record:
            continue
        found[str(record.get("SourceFile"))] = Current(
            _strings(record.get("Subject")),
            _strings(record.get("HierarchicalSubject")),
            " ".join(str(record.get("Description") or "").split()) or None,
        )
    return found


def edit_args(edit: Edit) -> list[str]:
    """Each keyword is removed first, so adding one that's there doesn't double it."""
    args = []
    for tag, add, remove in (
        (SUBJECT, edit.add_subject, edit.remove_subject),
        (HIERARCHY, edit.add_hierarchy, edit.remove_hierarchy),
    ):
        args += [f"{tag}-={k}" for k in (*remove, *add)]
        args += [f"{tag}+={k}" for k in add]
    if edit.set_description:
        args.append(f"{DESCRIPTION}={edit.description or ''}")
    return args


def apply_edits(command: str, edits: Mapping[str, Edit], *, cwd: Path, timeout_s: float) -> None:
    """Edit the sidecars in `cwd` in place, with one exiftool process for all of them.
    Arguments go through a file (one per line), so nothing lands on a command line."""
    if not edits:
        return
    lines: list[str] = []
    for name, edit in edits.items():
        lines += [*edit_args(edit), name, "-execute"]
    (cwd / "args.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    args = [command, "-@", "args.txt", "-common_args", "-q", "-overwrite_original"]
    _run(args, cwd=cwd, timeout_s=timeout_s)


# --- the action --------------------------------------------------------------------------


class SidecarItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=255)
    """The sidecar's file name, inside `folder`."""
    expected: str | None
    """The content hash the sidecar must still have; None: there must be none."""
    content: str = Field(max_length=MAX_CONTENT)
    file_id: str
    record: dict[str, Any]


class SyncParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    folder: LibraryPath
    items: list[SidecarItem] = Field(min_length=1, max_length=MAX_ITEMS)


def _join(folder: str, name: str) -> str:
    return f"{folder}{name}" if folder.endswith(":") else f"{folder}/{name}"


class SidecarSync:
    """Write finished sidecars of one folder. Their content and hashes are `transient`:
    the journal keeps the folder and the count, not a copy of every description. A
    sidecar that changed since it was read is skipped, and the next run merges again.
    Sidecars are derived data, so there's no undo: the next run would write them again."""

    name = ACTION
    params: type[BaseModel] = SyncParams
    ai_mode: AiMode = "proposal"
    sensitive = False

    def plan(self, params: BaseModel, view: ReadOnlyFS) -> Plan:
        assert isinstance(params, SyncParams)  # noqa: S101
        folder = view.state(params.folder)
        if folder.kind != "dir":
            raise ActionRefused(f"{params.folder} isn't a folder")
        for item in params.items:
            if "/" in item.name or "\\" in item.name or not is_sidecar(item.name):
                raise ActionRefused(f"{item.name!r} isn't a sidecar's name")
            photo = _join(params.folder, item.name[: -len(SIDECAR_SUFFIX)])
            if view.state(photo).kind != "file":
                raise ActionRefused(f"there's no {photo} for a sidecar")
            if not looks_like_xmp(item.content.encode()):
                raise ActionRefused(f"the content for {item.name!r} isn't XMP")
        count = len(params.items)
        return Plan(
            self.name,
            {"folder": params.folder, "count": count},
            f"Update {count} photo sidecar{'s' if count != 1 else ''} in {params.folder}",
            preconditions=(folder,),
            writes=(params.folder,),
            transient={"items": params.items},
        )

    def render(self, plan: Plan) -> Rendered:
        return Rendered(plan.summary)

    def apply(self, plan: Plan, fs: GuardedFS) -> Applied:
        folder = str(plan.params["folder"])
        written: list[str] = []
        items: list[SidecarItem] = list(plan.transient.get("items", ()))
        for item in items:
            path = _join(folder, item.name)
            try:
                if _write(fs, path, item):
                    written.append(path)
                    fs.write_state(_record(item, path[: -len(SIDECAR_SUFFIX)], fs.now()))
            except ActionRefused as exc:
                log.info(
                    "sidecar skipped",
                    extra={"fields": {"action": self.name, "error": type(exc).__name__}},
                )
        if not written:
            raise ActionRefused("every sidecar changed since it was read")
        return Applied(
            {"written": len(written), "skipped": len(items) - len(written)},
            changed=tuple(written),
        )

    def inverse(self, plan: Plan, applied: Applied, view: ReadOnlyFS) -> Plan | None:
        return None

    def examples(self) -> list[dict[str, Any]]:
        return [
            {
                "folder": "lib:docs",
                "items": [
                    {
                        "name": "a.txt.xmp",
                        "expected": None,
                        "content": EMPTY_XMP.decode(),
                        "file_id": "example",
                        "record": {},
                    }
                ],
            }
        ]


def _write(fs: GuardedFS, path: str, item: SidecarItem) -> bool:
    state = fs.state(path, content_hash=item.expected is not None)
    data = item.content.encode()
    if item.expected is None:
        if state.kind != "absent":
            return False
        fs.create(path, data)
        return True
    if state.kind != "file" or state.content_hash != item.expected:
        return False
    fs.replace(path, data)
    return True


def _record(item: SidecarItem, photo: str, now: Any) -> Any:
    def write(conn: Connection) -> None:
        save(conn, item.file_id, photo, item.record, now)

    return write


def save(conn: Connection, file_id: str, path: str, data: Mapping[str, Any], now: Any) -> None:
    values = {"path": path, "data": dict(data), "updated_at": now}
    conn.execute(
        insert(table)
        .values(file_id=file_id, **values)
        .on_conflict_do_update(index_elements=[table.c.file_id], set_=values)
    )


# --- the job -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Stored:
    path: str
    record: Record


@dataclass(slots=True)
class Stats:
    written: int = 0
    skipped: int = 0


@dataclass(frozen=True, slots=True)
class Syncer:
    command: str
    batch: int
    run_limit: int
    max_bytes: int
    timeout_s: int
    stats: Stats = field(default_factory=Stats)

    def run(self, ctx: JobContext) -> None:
        library, actions = ctx.library, ctx.actions
        if library is None or actions is None:
            raise PermanentJobError(f"{ctx.type} has to run on the executor lane")
        if library.rebuilding():
            return  # tags and descriptions not recomputed yet would look deleted
        revision = library.revision()
        with ctx.state.read() as conn:
            last = conn.execute(select(runs.c.revision)).scalar_one_or_none()
        if revision == last:
            return  # nothing changed, so don't compare every photo again
        after: str | None = None
        while self.stats.written < self.run_limit:
            page = library.files(kind="image", after=after, limit=PAGE, texts=(CAPTION,))
            todo = list(_stale(ctx.state, page.files))
            for batch in _batches(todo, self.batch):
                self._sync(ctx, library, actions, batch)
                ctx.renew_lease()
                if self.stats.written >= self.run_limit:
                    break
            after = page.next_after
            if after is None:
                break
        if self.stats.written < self.run_limit:
            with ctx.state.write() as conn:
                conn.execute(
                    insert(runs)
                    .values(name=RUN, revision=revision)
                    .on_conflict_do_update(
                        index_elements=[runs.c.name], set_={"revision": revision}
                    )
                )
        log.info(
            "sidecars synced",
            extra={"fields": {"written": self.stats.written, "skipped": self.stats.skipped}},
        )

    def _sync(
        self,
        ctx: JobContext,
        library: LibraryAccess,
        actions: ActionRunner,
        batch: Sequence[tuple[LibraryFile, Wanted, Stored | None]],
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="steward-xmp-") as tmp:
            work = Path(tmp)
            copies = self._copy(library, batch, work)
            existing = [c.name for c in copies if c.expected is not None]
            timeout = self.timeout_s + len(copies)
            try:
                current = read_current(self.command, existing, cwd=work, timeout_s=timeout)
                edits: dict[str, Edit] = {}
                records: dict[str, Record] = {}
                for copy in copies:
                    if copy.expected is not None and copy.name not in current:
                        self.stats.skipped += 1
                        continue
                    edit, record = merge(
                        copy.want,
                        copy.stored.record if copy.stored and copy.expected else None,
                        current.get(copy.name, Current()),
                    )
                    records[copy.name] = record
                    if not edit.empty:
                        edits[copy.name] = edit
                apply_edits(self.command, edits, cwd=work, timeout_s=timeout)
            except SidecarToolFailed:
                raise RetryLater("exiftool failed on a batch of sidecars", delay_s=600) from None
            self._finish(ctx, actions, copies, edits, records, work)

    def _copy(
        self,
        library: LibraryAccess,
        batch: Sequence[tuple[LibraryFile, Wanted, Stored | None]],
        work: Path,
    ) -> list[_Copy]:
        copies = []
        for i, (file, want, stored) in enumerate(batch):
            sidecar = file.path + SIDECAR_SUFFIX
            try:
                found = library.read(sidecar, max_bytes=self.max_bytes)
            except ActionRefused:
                self.stats.skipped += 1
                continue
            if found is not None and not looks_like_xmp(found[0]):
                self.stats.skipped += 1
                continue
            data, expected = found if found is not None else (EMPTY_XMP, None)
            name = f"{i:04d}.xmp"
            (work / name).write_bytes(data)
            copies.append(_Copy(name, file, want, stored, data, expected))
        return copies

    def _finish(
        self,
        ctx: JobContext,
        actions: ActionRunner,
        copies: Sequence[_Copy],
        edits: Mapping[str, Edit],
        records: Mapping[str, Record],
        work: Path,
    ) -> None:
        items = []
        unchanged = []
        for copy in copies:
            if copy.name not in records:
                continue
            if copy.name not in edits:
                unchanged.append(copy)
                continue
            data = (work / copy.name).read_bytes()
            if data == copy.data or not looks_like_xmp(data):
                self.stats.skipped += 1  # exiftool refused this one
                continue
            items.append(
                {
                    "name": copy.file.file.name + SIDECAR_SUFFIX,
                    "expected": copy.expected,
                    "content": data.decode(),
                    "file_id": copy.file.file.file_id,
                    "record": records[copy.name].as_dict(),
                }
            )
        if unchanged:
            with ctx.state.write() as conn:
                for copy in unchanged:
                    save(
                        conn,
                        copy.file.file.file_id,
                        copy.file.path,
                        records[copy.name].as_dict(),
                        utcnow(),
                    )
        if not items:
            return
        folder = (
            copies[0].file.path.rsplit("/", 1)[0]
            if "/" in copies[0].file.file.relpath
            else f"{copies[0].file.file.library_id}:"
        )
        try:
            outcome = actions.run(ACTION, {"folder": folder, "items": items})
        except ActionRefused as exc:
            self.stats.skipped += len(items)
            log.info(
                "sidecar batch refused",
                extra={"fields": {"error": type(exc).__name__}},
            )
            return
        self.stats.written += int(outcome.result.get("written", 0))
        self.stats.skipped += int(outcome.result.get("skipped", 0))


@dataclass(frozen=True, slots=True)
class _Copy:
    name: str
    """Its fixed name in the temp folder."""
    file: LibraryFile
    want: Wanted
    stored: Stored | None
    data: bytes
    expected: str | None


def _stale(
    state: StateAccess, files: Sequence[LibraryFile]
) -> Iterator[tuple[LibraryFile, Wanted, Stored | None]]:
    """The photos in `files` whose sidecar doesn't say what it should, by folder."""
    eligible = [f for f in files if f.folder.sidecars and f.folder.writable and not f.folder.locked]
    with state.read() as conn:
        stored = {
            r.file_id: Stored(r.path, Record.from_dict(r.data))
            for r in conn.execute(
                select(table).where(table.c.file_id.in_([f.file.file_id for f in eligible]))
            )
        }
    for file in sorted(eligible, key=lambda f: f.path):
        want = wanted(file.tags, file.texts.get(CAPTION))
        known = stored.get(file.file.file_id)
        if known is None and want.empty:
            continue
        if known is not None and known.path == file.path and known.record.matches(want):
            continue
        yield file, want, known


def _folder(file: LibraryFile) -> tuple[str, str]:
    return file.file.library_id, file.file.relpath.rsplit("/", 1)[
        0
    ] if "/" in file.file.relpath else ""


def _batches(
    todo: Sequence[tuple[LibraryFile, Wanted, Stored | None]], size: int
) -> Iterator[Sequence[tuple[LibraryFile, Wanted, Stored | None]]]:
    """At most `size` photos at a time, all in one folder."""
    for _, group in groupby(todo, key=lambda item: _folder(item[0])):
        items = list(group)
        for start in range(0, len(items), size):
            yield items[start : start + size]


def export(conn: Connection) -> dict[str, list[dict[str, Any]]]:
    return {"photos_sidecars": [dict(r._mapping) for r in conn.execute(select(table))]}
