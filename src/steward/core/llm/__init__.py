# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The LLM layer: roles mapped to endpoints, behind the privacy gateway."""

from steward.core.llm.gateway import (
    Gateway,
    GatewayResult,
    PrivacyViolation,
    Purpose,
    RoleNotConfigured,
    Route,
    check_llm_config,
    model_key,
)
from steward.core.llm.transport import (
    ChatRequest,
    EmbedRequest,
    ModelRequest,
    ModelResponse,
    ModelTransport,
    ModelUnavailable,
    OpenAICompatibleTransport,
)

__all__ = [
    "ChatRequest",
    "EmbedRequest",
    "Gateway",
    "GatewayResult",
    "ModelRequest",
    "ModelResponse",
    "ModelTransport",
    "ModelUnavailable",
    "OpenAICompatibleTransport",
    "PrivacyViolation",
    "Purpose",
    "RoleNotConfigured",
    "Route",
    "check_llm_config",
    "model_key",
]
