# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The public SDK: the only part of steward that modules may import.

Everything a module needs is re-exported here. Before 1.0 this surface may change in
any release (with a CHANGELOG entry); from 1.0, breaking it needs a major version.
"""

from steward.sdk.health import HealthCheck, HealthResult, HealthStatus
from steward.sdk.module import MODULE_NAME_PATTERN, Module, ModuleConfig, ModuleKind
from steward.sdk.registry import Registration, RegistrationConflict, Registrations, Registry

__all__ = [
    "MODULE_NAME_PATTERN",
    "HealthCheck",
    "HealthResult",
    "HealthStatus",
    "Module",
    "ModuleConfig",
    "ModuleKind",
    "Registration",
    "RegistrationConflict",
    "Registrations",
    "Registry",
]
