# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`JobContext.library` and `JobContext.actions` (`steward.sdk.library`)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import func, select

from steward.core.db import Databases
from steward.core.db.schema import events, stage_runs
from steward.core.executor import Executor
from steward.core.executor.guarded import ReadOnlyView, content_hash
from steward.core.folders import load_policy
from steward.core.privacy import PrivacyRules
from steward.core.privacy.classify import Classifier
from steward.core.search import Visibility, connect
from steward.core.stages.planner import folder_info
from steward.core.tags import effective
from steward.sdk import (
    ActionOutcome,
    ActionRefused,
    BlobInfo,
    FileInfo,
    FileTag,
    LibraryFile,
    LibraryPage,
)

_FILES = """
SELECT f.file_id, f.library_id, f.relpath, f.size AS file_size, f.mtime_ns, f.content_hash,
       b.hash_kind, b.size, b.mime, b.kind
FROM files f JOIN idx.blobs b ON b.content_hash = f.content_hash
WHERE f.present = 1 AND b.kind = ? AND f.file_id > ?
ORDER BY f.file_id LIMIT ?
"""
_COLUMNS = (
    "file_id",
    "library_id",
    "relpath",
    "file_size",
    "mtime_ns",
    "content_hash",
    "hash_kind",
    "size",
    "mime",
    "kind",
)
_BATCH = 500


class Library:
    def __init__(self, dbs: Databases, rules: PrivacyRules) -> None:
        self.dbs = dbs
        self.rules = rules

    def revision(self) -> str:
        with self.dbs.state.read() as state:
            event = state.execute(select(func.max(events.c.event_id))).scalar()
        with self.dbs.index.read() as index:
            run = index.execute(select(func.max(stage_runs.c.updated_at))).scalar()
        return f"{event}:{run}"

    def files(
        self,
        *,
        kind: str,
        after: str | None = None,
        limit: int = 500,
        texts: tuple[str, ...] = (),
    ) -> LibraryPage:
        with self.dbs.state.read() as state:
            classifier = Classifier.load(state, self.rules)
        policy = classifier.policy
        visibility = Visibility(policy)
        with connect(self.dbs) as conn:
            raw = conn.execute(_FILES, (kind, after or "", limit)).fetchall()
        next_after = str(raw[-1][0]) if len(raw) == limit else None
        rows = [dict(zip(_COLUMNS, row, strict=True)) for row in raw]
        shown = [
            row
            for row in rows
            if not visibility.is_hidden(row["library_id"], row["relpath"])
            and classifier.for_path(
                row["library_id"], row["relpath"], file_id=row["file_id"]
            ).sensitivity
            != "excluded"
        ]
        tags = self._tags(shown, policy)
        bodies = self._texts(shown, texts)
        found = []
        for row in shown:
            folder = classifier.folder_settings(row["library_id"], _parent(row["relpath"]))
            if folder is None:
                continue
            found.append(
                LibraryFile(
                    file=FileInfo(
                        row["file_id"],
                        row["library_id"],
                        row["relpath"],
                        row["file_size"],
                        row["mtime_ns"],
                    ),
                    blob=BlobInfo(
                        row["content_hash"],
                        row["hash_kind"],
                        row["size"],
                        row["mime"],
                        row["kind"],
                    ),
                    folder=folder_info(folder),
                    tags=tuple(tags[row["file_id"]]),
                    texts=bodies.get(row["content_hash"], {}),
                )
            )
        return LibraryPage(found, next_after)

    def _tags(self, rows: Sequence[Mapping[str, Any]], policy: Any) -> dict[str, list[FileTag]]:
        refs = [
            effective.FileRef(r["file_id"], r["content_hash"], r["library_id"], r["relpath"])
            for r in rows
        ]
        with self.dbs.state.read() as state, self.dbs.index.read() as index:
            return effective.many(state, index, policy, refs)

    def _texts(
        self, rows: Sequence[Mapping[str, Any]], artifacts: tuple[str, ...]
    ) -> dict[str, dict[str, str]]:
        if not artifacts or not rows:
            return {}
        hashes = list({r["content_hash"] for r in rows})
        found: dict[str, dict[str, list[str]]] = {}
        marks = ",".join("?" * len(artifacts))
        with connect(self.dbs) as conn:
            for start in range(0, len(hashes), _BATCH):
                batch = hashes[start : start + _BATCH]
                query = (
                    "SELECT content_hash, artifact, body FROM idx.texts "  # noqa: S608 -- only marks
                    f"WHERE artifact IN ({marks}) AND content_hash IN "
                    f"({','.join('?' * len(batch))}) ORDER BY content_hash, artifact, part"
                )
                for content, artifact, body in conn.execute(query, (*artifacts, *batch)):
                    found.setdefault(content, {}).setdefault(artifact, []).append(body)
        return {
            content: {artifact: "\n".join(parts).strip() for artifact, parts in by_artifact.items()}
            for content, by_artifact in found.items()
        }

    def read(self, path: str, *, max_bytes: int) -> tuple[bytes, str] | None:
        with self.dbs.state.read() as conn:
            view = ReadOnlyView(load_policy(conn), self.dbs.state, rules=self.rules)
        state = view.state(path)
        if state.kind == "absent":
            return None
        if state.kind != "file":
            raise ActionRefused(f"{path} isn't a file")
        data = view.read(path, max_bytes=max_bytes)
        return data, content_hash(data)


class ModuleActions:
    """Actions run by a module's job. Never `confirmed`: nobody checked a password."""

    def __init__(self, executor: Executor, module: str) -> None:
        self.executor = executor
        self.actor = f"module:{module}"

    def run(self, action: str, params: Mapping[str, Any]) -> ActionOutcome:
        entry = self.executor.run(action, params, actor=self.actor)
        return ActionOutcome(entry.entry_id, entry.result)


def _parent(relpath: str) -> str:
    return relpath.rsplit("/", 1)[0] if "/" in relpath else ""
