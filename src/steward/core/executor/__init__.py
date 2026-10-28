# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The executor: the only code that writes to libraries (docs/adr/0005, 0014).

`Executor` runs registered action types through `Guarded`, journals them and undoes
them; `actions` holds the core's own action types.
"""

from steward.core.executor.actions import BUILTIN_ACTIONS, MAX_TEXT_BYTES, register_actions
from steward.core.executor.dirs import WRITE_LOCK, ExecutorError, create_folder
from steward.core.executor.executor import (
    ActionFailed,
    Executor,
    JournalEntry,
    Preview,
    SensitiveActionError,
)
from steward.core.executor.guarded import LOCKED_TRASH, TEMP_PREFIX, Guarded, ReadOnlyView

__all__ = [
    "BUILTIN_ACTIONS",
    "LOCKED_TRASH",
    "MAX_TEXT_BYTES",
    "TEMP_PREFIX",
    "WRITE_LOCK",
    "ActionFailed",
    "Executor",
    "ExecutorError",
    "Guarded",
    "JournalEntry",
    "Preview",
    "ReadOnlyView",
    "SensitiveActionError",
    "create_folder",
    "register_actions",
]
