# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The watcher: inotify (via watchdog) on every library root, turned into debounced
`core.scan` jobs. It only speeds things up; the periodic full scan is what makes
discovery correct, so missed events (overflow, too many folders to watch, a watcher
that was down) only delay changes until the next scan.

Each changed path waits `debounce_seconds` after its last event, then all due paths of
a library go into one scan job. A move's source and destination are both scanned; the
scanner matches them up.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver, ObservedWatch

from steward.core import queue
from steward.core.config import DiscoverySettings
from steward.core.db import Databases
from steward.core.db.schema import libraries
from steward.core.discovery.filters import is_ignored

log = logging.getLogger(__name__)

SCAN_JOB = "core.scan"
WATCHER_PRIORITY = 10
"""Above full scans, so changes you just made are picked up first."""
MAX_PATHS_PER_JOB = 500
REFRESH_SECONDS = 60
IGNORED_EVENTS = frozenset({"opened", "closed_no_write"})


@dataclass(frozen=True, slots=True)
class _Watched:
    root: str
    watch: ObservedWatch | None


class Watcher:
    def __init__(
        self,
        dbs: Databases,
        settings: DiscoverySettings,
        *,
        max_attempts: int = 5,
        observer: BaseObserver | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.dbs = dbs
        self.settings = settings
        self.max_attempts = max_attempts
        self.observer = observer or Observer()
        self.clock = clock
        self.watched: dict[str, _Watched] = {}
        self._pending: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    # --- events -> pending paths ----------------------------------------------------

    def path_changed(self, library_id: str, root: str, path: str) -> None:
        relpath = os.path.relpath(path, root)
        if relpath == ".":
            relpath = ""
        if relpath == ".." or relpath.startswith(".." + os.sep):
            return
        relpath = relpath.replace(os.sep, "/")
        if relpath and is_ignored(relpath, self.settings.ignore_patterns):
            return
        with self._lock:
            self._pending[(library_id, relpath)] = self.clock() + self.settings.debounce_seconds

    def flush(self, *, force: bool = False) -> int:
        """Enqueue scan jobs for paths whose debounce time has passed. Returns jobs added."""
        now = self.clock()
        due: dict[str, list[str]] = defaultdict(list)
        with self._lock:
            for key, deadline in list(self._pending.items()):
                if force or deadline <= now:
                    due[key[0]].append(key[1])
                    del self._pending[key]
        if not due:
            return 0
        added = 0
        with self.dbs.state.write() as conn:
            for library_id, paths in due.items():
                paths.sort()
                for i in range(0, len(paths), MAX_PATHS_PER_JOB):
                    queue.enqueue(
                        conn,
                        SCAN_JOB,
                        lane="io",
                        payload={
                            "library_id": library_id,
                            "paths": paths[i : i + MAX_PATHS_PER_JOB],
                        },
                        priority=WATCHER_PRIORITY,
                        max_attempts=self.max_attempts,
                    )
                    added += 1
        return added

    # --- which folders are watched -----------------------------------------------------

    def refresh(self) -> None:
        """Watch every library root; follow added, moved and removed libraries."""
        with self.dbs.state.read() as conn:
            roots = {
                r.library_id: r.root
                for r in conn.execute(select(libraries.c.library_id, libraries.c.root))
            }
        for library_id, current in list(self.watched.items()):
            if roots.get(library_id) != current.root:
                if current.watch is not None:
                    self.observer.unschedule(current.watch)
                del self.watched[library_id]
        for library_id, root in roots.items():
            if library_id in self.watched or not Path(root).is_dir():
                continue
            try:
                watch = self.observer.schedule(
                    _Handler(self, library_id, root), root, recursive=True
                )
            except OSError as exc:
                # Usually the inotify watch limit (fs.inotify.max_user_watches). Scans
                # still find every change; they're just not immediate.
                log.warning(
                    "can't watch library; changes will be found by the periodic scan",
                    extra={"fields": {"library_id": library_id, "error": type(exc).__name__}},
                )
                watch = None
            self.watched[library_id] = _Watched(root, watch)

    def run(self, stop: threading.Event) -> None:
        self.refresh()
        self.observer.start()
        last_refresh = self.clock()
        try:
            while not stop.wait(1.0):
                try:
                    self.flush()
                    if self.clock() - last_refresh >= REFRESH_SECONDS:
                        self.refresh()
                        last_refresh = self.clock()
                except Exception:
                    log.exception("watcher error")
        finally:
            self.observer.stop()
            self.observer.join(timeout=10)
            try:
                self.flush(force=True)
            except Exception:
                log.exception("watcher error")


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: Watcher, library_id: str, root: str) -> None:
        self.watcher = watcher
        self.library_id = library_id
        self.root = root

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in IGNORED_EVENTS:
            return
        # A folder's "modified" just means an entry inside changed; that entry has its
        # own event, and scanning the whole folder again would be wasted work.
        if event.is_directory and event.event_type == "modified":
            return
        self.watcher.path_changed(self.library_id, self.root, os.fsdecode(event.src_path))
        if event.dest_path:
            self.watcher.path_changed(self.library_id, self.root, os.fsdecode(event.dest_path))
