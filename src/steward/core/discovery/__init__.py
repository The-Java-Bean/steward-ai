# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Discovery: finding files and folders in libraries, hashing them, and keeping their
identities across edits, renames and moves (plan §8.4, §10.1)."""

from steward.core.discovery.hashing import HashResult, detect_mime, hash_file, kind_of
from steward.core.discovery.jobs import (
    RECONCILE_JOB,
    enqueue_full_scan,
    enqueue_scan,
    register_discovery,
)
from steward.core.discovery.scanner import ScanError, Scanner, ScanReport
from steward.core.discovery.watcher import SCAN_JOB, Watcher

__all__ = [
    "RECONCILE_JOB",
    "SCAN_JOB",
    "HashResult",
    "ScanError",
    "ScanReport",
    "Scanner",
    "Watcher",
    "detect_mime",
    "enqueue_full_scan",
    "enqueue_scan",
    "hash_file",
    "kind_of",
    "register_discovery",
]
