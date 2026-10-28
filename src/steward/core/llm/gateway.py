# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""The privacy gateway: the one place every model call passes through (docs/adr/0004).

`Gateway.call(role, request, provenance=..., purpose=...)`:

1. `provenance` lists every file whose content (text, chunk, caption or filename) is in
   the request. It's required; an empty list says "no file content".
2. The gateway classifies those files (`steward.core.privacy`) and takes the highest
   sensitivity, including the conversation's `taint`. Unknown files count as
   `local-only` and locked.
3. `excluded` anywhere: refused (`PrivacyViolation`); it means a bug upstream.
4. `purpose="chat"` (anything a chat surface shows) with a locked file: refused.
5. `local-only` to a `remote` endpoint: refused, unless the caller allows the role's
   `local_fallback` (chat surfaces do; background jobs don't), which is then used and
   reported. Never a silent re-route.

Logs carry roles, endpoint names, counts and levels, never content.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from steward.core.config import EndpointConfig, LlmConfig, RoleConfig
from steward.core.db import Database
from steward.core.folders.types import SENSITIVITY_ORDER, Sensitivity, max_sensitivity
from steward.core.llm.transport import (
    ModelRequest,
    ModelResponse,
    ModelTransport,
    OpenAICompatibleTransport,
)
from steward.core.privacy import Classifier, PrivacyRules

log = logging.getLogger(__name__)

type Purpose = Literal["index", "chat"]
"""`index`: background processing (results stay in the index). `chat`: the answer is
shown on a chat surface, so locked content may not be in it."""


class PrivacyViolation(Exception):
    """The request may not go to this endpoint. Never retried elsewhere silently."""


class RoleNotConfigured(LookupError):
    """No endpoint is mapped to the role; features needing it stay off."""


@dataclass(frozen=True, slots=True)
class Route:
    role: str
    endpoint_name: str
    endpoint: EndpointConfig
    model: str
    sensitivity: Sensitivity
    """Highest sensitivity of the provenance and the taint."""
    taint: Sensitivity
    """The conversation's taint after this call (only chat calls raise it)."""
    switched_from: str | None = None
    """The role that was asked for, if privacy forced its `local_fallback`."""
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class GatewayResult:
    response: ModelResponse
    route: Route


class Gateway:
    def __init__(
        self,
        config: LlmConfig,
        state_db: Database,
        privacy: PrivacyRules,
        transport: ModelTransport | None = None,
    ) -> None:
        self.config = config
        self.state_db = state_db
        self.privacy = privacy
        self.transport = transport if transport is not None else OpenAICompatibleTransport()
        self._slots = {
            name: threading.BoundedSemaphore(ep.max_concurrency)
            for name, ep in config.endpoints.items()
        }

    def has_role(self, role: str) -> bool:
        return role in self.config.roles

    def route(
        self,
        role: str,
        *,
        provenance: Collection[str],
        purpose: Purpose,
        taint: Sensitivity = "normal",
        allow_local_fallback: bool = False,
    ) -> Route:
        """Decide where a request may go, or raise `PrivacyViolation`/`RoleNotConfigured`."""
        if isinstance(provenance, str) or not all(isinstance(f, str) for f in provenance):
            raise TypeError("provenance must be a collection of file ids")
        if taint not in SENSITIVITY_ORDER:
            raise PrivacyViolation(f"unknown taint {taint!r}")
        role_cfg = self._role(role)
        with self.state_db.read() as conn:
            classes = Classifier.load(conn, self.privacy).for_files(conn, provenance)
        level = max_sensitivity(taint, *(c.sensitivity for c in classes.values()))
        if level == "excluded":
            raise PrivacyViolation("the request contains excluded content")
        if purpose == "chat" and any(c.locked for c in classes.values()):
            raise PrivacyViolation("locked files can't reach a chat surface")
        new_taint = level if purpose == "chat" else taint
        endpoint = self.config.endpoints[role_cfg.endpoint]
        if level == "local-only" and endpoint.trust != "local":
            fallback = role_cfg.local_fallback
            if not allow_local_fallback or fallback is None:
                raise PrivacyViolation(
                    f"local-only content can't go to role {role!r} (remote endpoint "
                    f"{role_cfg.endpoint!r})"
                )
            target = self._role(fallback)
            return Route(
                fallback,
                target.endpoint,
                self.config.endpoints[target.endpoint],
                target.model,
                level,
                new_taint,
                switched_from=role,
                reason="the conversation includes local-only content",
            )
        return Route(role, role_cfg.endpoint, endpoint, role_cfg.model, level, new_taint)

    def call(
        self,
        role: str,
        request: ModelRequest,
        *,
        provenance: Collection[str],
        purpose: Purpose,
        taint: Sensitivity = "normal",
        allow_local_fallback: bool = False,
    ) -> GatewayResult:
        route = self.route(
            role,
            provenance=provenance,
            purpose=purpose,
            taint=taint,
            allow_local_fallback=allow_local_fallback,
        )
        fields = {
            "role": route.role,
            "endpoint": route.endpoint_name,
            "files": len(provenance),
            "sensitivity": route.sensitivity,
            "purpose": purpose,
        }
        if route.switched_from:
            fields["switched_from"] = route.switched_from
        log.info("model call", extra={"fields": fields})
        with self._slots[route.endpoint_name]:
            response = self.transport.send(route.endpoint, route.model, request)
        return GatewayResult(response, route)

    def _role(self, role: str) -> RoleConfig:
        try:
            return self.config.roles[role]
        except KeyError:
            raise RoleNotConfigured(f"no model is configured for role {role!r}") from None


def check_llm_config(config: LlmConfig) -> None:
    """Startup checks beyond the schema. Remote endpoints arrive with a later module
    (`remote_llm`); until then they're refused, so nothing can be sent off the machine."""
    remote = sorted(n for n, ep in config.endpoints.items() if ep.trust == "remote")
    if remote:
        raise ValueError(
            f"llm.yaml: endpoint(s) {', '.join(remote)} have trust: remote; remote endpoints "
            "need the remote_llm module, which isn't available yet"
        )
