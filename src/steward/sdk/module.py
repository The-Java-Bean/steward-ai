# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The module contract: what every steward module (built-in or not) provides."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from steward.sdk.registry import Registry

ModuleKind = Literal["feature", "integration"]

# Module names are snake_case so one name works as the entry-point name, the Python
# package, the config key and the `<module>_` table prefix.
MODULE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class ModuleConfig(BaseModel):
    """Base class for a module's config section. Unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)


@runtime_checkable
class Module(Protocol):
    """A steward module, discovered through the `steward.modules` entry-point group.

    The entry point names the module class; the loader instantiates it with no
    arguments and calls `register` once, after every module in `requires`.
    """

    name: ClassVar[str]
    version: ClassVar[str]
    kind: ClassVar[ModuleKind]
    requires: ClassVar[tuple[str, ...]]
    config_model: ClassVar[type[ModuleConfig] | None]

    def register(self, r: Registry) -> None:
        """Add this module's extensions to `r`. Must not do I/O or start work."""
        ...
