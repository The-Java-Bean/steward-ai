# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The planner: decides which stage jobs a piece of content needs, and queues them.

It works from the current state (`files`, `blobs`, `stage_runs`), never by replaying
events, so planning the same content twice is harmless and a full pass can always
repair anything missed (docs/adr/0011-stage-framework.md).

For each content hash, a stage is **wanted** when at least one present file with that
content is in a folder where the stage is enabled (folder `index` policy and type
`stages` add/remove) and `applies_to` says yes. A result is **current** when it was made
by the stage's current version (and, for stages that call a model, the current model)
after every input it read was last recomputed; it's
**usable** when it's current and `done`. A stage is **pending** when it's wanted, not
current, and each stage it requires is usable or pending itself. A pending stage is
queued once every stage it requires is usable and none of its optional inputs is
pending. Failed results at the current version aren't re-queued (the job's retries
already ran; `steward jobs retry` or a version bump runs them again).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, RowMapping, select

from steward.core import queue
from steward.core.db import Databases, utcnow
from steward.core.db.schema import blobs, files, stage_runs
from steward.core.folders import EffectiveSettings, FolderRef, PathError, Policy, load_policy
from steward.core.privacy import PrivacyRules
from steward.core.stages.graph import StageGraph
from steward.sdk import BlobInfo, FileInfo, FolderInfo, Stage

log = logging.getLogger(__name__)

STAGE_JOB_PREFIX = "core.stage."
PLAN_BATCH = 500


def stage_job_type(stage: str) -> str:
    return STAGE_JOB_PREFIX + stage


@dataclass(frozen=True, slots=True)
class RunRecord:
    stage_version: int
    status: str
    updated_at: datetime
    model: str | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    """A file a stage may run on, with the folder settings that allowed it."""

    file: FileInfo
    folder: FolderInfo
    root: str
    """The library root the file's relpath is under."""


def folder_info(settings: EffectiveSettings) -> FolderInfo:
    return FolderInfo(
        type=settings.type.name,
        index=settings.index,
        sensitivity=settings.sensitivity,
        locked=settings.locked,
        fields=settings.fields,
        default_tags=settings.default_tags,
        stages_added=settings.stages_add,
    )


def blob_info(row: RowMapping) -> BlobInfo:
    return BlobInfo(
        content_hash=str(row["content_hash"]),
        hash_kind=row["hash_kind"],
        size=int(row["size"]),
        mime=str(row["mime"]),
        kind=str(row["kind"]),
    )


def file_info(row: RowMapping) -> FileInfo:
    return FileInfo(
        file_id=str(row["file_id"]),
        library_id=str(row["library_id"]),
        relpath=str(row["relpath"]),
        size=int(row["size"]),
        mtime_ns=int(row["mtime_ns"]),
    )


class Eligibility:
    """Folder-settings and `applies_to` checks, with settings cached per folder. Built
    from one policy snapshot; make a new one per planning batch."""

    def __init__(
        self, graph: StageGraph, policy: Policy, privacy: PrivacyRules | None = None
    ) -> None:
        self.graph = graph
        self.policy = policy
        self.privacy = privacy if privacy is not None else PrivacyRules.default()
        self._settings: dict[tuple[str, str], EffectiveSettings | None] = {}
        self._enabled: dict[tuple[str, str], set[str]] = {}

    def settings(self, file: FileInfo) -> EffectiveSettings | None:
        folder = file.relpath.rsplit("/", 1)[0] if "/" in file.relpath else ""
        key = (file.library_id, folder)
        if key not in self._settings:
            try:
                self._settings[key] = self.policy.settings_for(FolderRef(*key))
            except PathError:
                self._settings[key] = None  # library removed since the file was seen
        return self._settings[key]

    def enabled_stages(self, file: FileInfo) -> set[str]:
        """Stages the file's folder allows (before `applies_to`)."""
        settings = self.settings(file)
        if settings is None or settings.index == "none":
            return set()
        # Excluded content is never read. Discovery doesn't hash such files, so this only
        # matters between a rule change and the rescan that drops their hash.
        if settings.sensitivity == "excluded" or self.privacy.is_secret(file.relpath):
            return set()
        if self.privacy.level_for(file.relpath) == "excluded":
            return set()
        key = (file.library_id, settings.ref.relpath)
        if key not in self._enabled:
            added = self.graph.matching(settings.stages_add)
            removed = self.graph.matching(settings.stages_remove)
            self._enabled[key] = {
                s.name
                for s in self.graph.stages.values()
                if (s.default or s.name in added)
                and s.name not in removed
                and (s.index == "metadata" or settings.index == "full")
            }
        return self._enabled[key]

    def candidates(self, stage: Stage, blob: BlobInfo, rows: Iterable[FileInfo]) -> list[Candidate]:
        found = []
        for file in rows:
            if stage.name not in self.enabled_stages(file):
                continue
            settings = self.settings(file)
            if settings is None:  # unreachable: enabled_stages() is empty then
                continue
            folder = folder_info(settings)
            try:
                applies = stage.applies_to(blob, file, folder)
            except Exception:
                log.warning(
                    "applies_to raised; treating as not applicable",
                    exc_info=True,
                    extra={"fields": {"stage": stage.name, "content_hash": blob.content_hash}},
                )
                applies = False
            if applies:
                root = self.policy.library(file.library_id).root
                found.append(Candidate(file, folder, root))
        return found


def run_status(graph: StageGraph, runs: Mapping[str, RunRecord]) -> tuple[set[str], set[str]]:
    """(current, usable) stage names for one content hash."""
    current: set[str] = set()
    usable: set[str] = set()
    for name, stage in graph.stages.items():
        run = runs.get(name)
        if run is None:
            continue
        if name == graph.root:
            ok = run.status == "done"
        else:
            ups = graph.upstream(name)
            optional = [up for up in graph.optional_upstream(name) if up in current]
            key = graph.model_keys.get(name)
            ok = (
                run.stage_version == stage.version
                and (key is None or run.model == key)
                and all(up in usable for up in ups)
                and all(runs[up].updated_at <= run.updated_at for up in (*ups, *optional))
            )
        if ok:
            current.add(name)
            if run.status == "done":
                usable.add(name)
    return current, usable


class Planner:
    def __init__(
        self,
        dbs: Databases,
        graph: StageGraph,
        *,
        max_attempts: int = 5,
        clock: Callable[[], datetime] = utcnow,
        privacy: PrivacyRules | None = None,
    ) -> None:
        self.dbs = dbs
        self.graph = graph
        self.privacy = privacy if privacy is not None else PrivacyRules.default()
        self.max_attempts = max_attempts
        self.clock = clock

    # --- loading ------------------------------------------------------------------

    def load(
        self, hashes: Sequence[str]
    ) -> tuple[
        Policy, dict[str, list[FileInfo]], dict[str, BlobInfo], dict[str, dict[str, RunRecord]]
    ]:
        with self.dbs.state.read() as conn:
            policy = load_policy(conn)
            by_hash = self.present_files(conn, hashes)
        with self.dbs.index.read() as conn:
            blob_rows = {
                r["content_hash"]: blob_info(r)
                for r in conn.execute(
                    select(blobs).where(blobs.c.content_hash.in_(hashes))
                ).mappings()
            }
            runs: dict[str, dict[str, RunRecord]] = {h: {} for h in hashes}
            for r in conn.execute(
                select(
                    stage_runs.c.content_hash,
                    stage_runs.c.stage,
                    stage_runs.c.stage_version,
                    stage_runs.c.status,
                    stage_runs.c.updated_at,
                    stage_runs.c.model,
                ).where(stage_runs.c.content_hash.in_(hashes))
            ):
                runs[r.content_hash][r.stage] = RunRecord(
                    r.stage_version, r.status, r.updated_at, r.model
                )
        return policy, by_hash, blob_rows, runs

    @staticmethod
    def present_files(conn: Connection, hashes: Sequence[str]) -> dict[str, list[FileInfo]]:
        by_hash: dict[str, list[FileInfo]] = {h: [] for h in hashes}
        rows = conn.execute(
            select(files)
            .where(files.c.content_hash.in_(hashes), files.c.present.is_(True))
            .order_by(files.c.file_id)
        ).mappings()
        for r in rows:
            by_hash[r["content_hash"]].append(file_info(r))
        return by_hash

    # --- planning -----------------------------------------------------------------

    def ready(
        self,
        eligibility: Eligibility,
        blob: BlobInfo,
        rows: Sequence[FileInfo],
        runs: Mapping[str, RunRecord],
    ) -> list[str]:
        """Stages to queue for one content hash."""
        if not rows:
            return []
        graph = self.graph
        current, usable = run_status(graph, runs)
        pending: list[str] = []
        for name, stage in graph.stages.items():
            if name == graph.root or name in current:
                continue
            if not all(up in usable or up in pending for up in graph.upstream(name)):
                continue
            if eligibility.candidates(stage, blob, rows):
                pending.append(name)
        return [
            name
            for name in pending
            if all(up in usable for up in graph.upstream(name))
            and not any(up in pending for up in graph.optional_upstream(name))
        ]

    def plan_hashes(self, hashes: Iterable[str]) -> int:
        """Queue every stage job these contents need now. Returns jobs queued (or
        already queued)."""
        pending = sorted(set(hashes))
        total = 0
        for i in range(0, len(pending), PLAN_BATCH):
            batch = pending[i : i + PLAN_BATCH]
            policy, by_hash, blob_rows, runs = self.load(batch)
            eligibility = Eligibility(self.graph, policy, self.privacy)
            todo = [
                (h, name)
                for h in batch
                if h in blob_rows
                for name in self.ready(eligibility, blob_rows[h], by_hash[h], runs[h])
            ]
            if todo:
                with self.dbs.state.write() as conn:
                    for content_hash, name in todo:
                        self.enqueue(conn, name, content_hash)
            total += len(todo)
        return total

    def plan_files(self, file_ids: Iterable[str]) -> int:
        ids = sorted(set(file_ids))
        hashes: set[str] = set()
        with self.dbs.state.read() as conn:
            for i in range(0, len(ids), PLAN_BATCH):
                hashes.update(
                    conn.execute(
                        select(files.c.content_hash).where(
                            files.c.file_id.in_(ids[i : i + PLAN_BATCH]),
                            files.c.content_hash.is_not(None),
                        )
                    ).scalars()
                )
        return self.plan_hashes(hashes)

    def plan_all(self, *, renew: Callable[[], None] | None = None) -> int:
        """Plan every content that a present file has, in batches."""
        total = 0
        after = ""
        while True:
            with self.dbs.state.read() as conn:
                batch = list(
                    conn.execute(
                        select(files.c.content_hash)
                        .where(
                            files.c.present.is_(True),
                            files.c.content_hash.is_not(None),
                            files.c.content_hash > after,
                        )
                        .group_by(files.c.content_hash)
                        .order_by(files.c.content_hash)
                        .limit(PLAN_BATCH)
                    ).scalars()
                )
            if not batch:
                return total
            total += self.plan_hashes(batch)
            after = batch[-1]
            if renew is not None:
                renew()

    def enqueue(self, conn: Connection, stage: str, content_hash: str) -> int:
        return queue.enqueue(
            conn,
            stage_job_type(stage),
            lane=self.graph.stages[stage].lane,
            payload={"content_hash": content_hash},
            dedupe_key=f"stage:{stage}:{content_hash}",
            max_attempts=self.max_attempts,
            now=self.clock(),
        )
