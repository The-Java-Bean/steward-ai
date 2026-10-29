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

An endpoint that just failed is treated as down for a while (30 s, doubling up to
15 min, reset by a success): calls fail fast with `ModelUnavailable` instead of each
waiting for a timeout.

Logs carry roles, endpoint names, counts and levels, never content.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import threading
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Literal

from steward.core.config import EndpointConfig, LlmConfig, RoleConfig
from steward.core.db import Database
from steward.core.folders.types import SENSITIVITY_ORDER, Sensitivity, max_sensitivity
from steward.core.llm.transport import (
    ChatRequest,
    ModelRequest,
    ModelResponse,
    ModelTransport,
    ModelUnavailable,
    OpenAICompatibleTransport,
)
from steward.core.privacy import Classifier, PrivacyRules

log = logging.getLogger(__name__)

BREAKER_FIRST_S = 30.0
BREAKER_MAX_S = 900.0

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
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.state_db = state_db
        self.privacy = privacy
        self.transport = transport if transport is not None else OpenAICompatibleTransport()
        self.clock = clock
        self._slots = {
            name: threading.BoundedSemaphore(ep.max_concurrency)
            for name, ep in config.endpoints.items()
        }
        self._lock = threading.Lock()
        self._down: dict[str, tuple[float, float]] = {}
        """Endpoint -> (down until, current back-off) after a failure."""

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
        name = route.endpoint_name
        wait = self.down_for(name)
        if wait > 0:
            raise ModelUnavailable(
                f"endpoint {name!r} failed recently; trying again in {wait:.0f} s",
                retry_after_s=wait,
            )
        log.info("model call", extra={"fields": fields})
        request = with_reasoning(request, self.role_config(route.role))
        try:
            with self._slots[name]:
                response = self.transport.send(route.endpoint, route.model, request)
        except ModelUnavailable as exc:
            backoff = self._mark_down(name)
            log.warning(
                "model endpoint unavailable",
                extra={"fields": {**fields, "error": str(exc), "retry_after_s": backoff}},
            )
            raise ModelUnavailable(str(exc), retry_after_s=backoff) from None
        with self._lock:
            self._down.pop(name, None)
        return GatewayResult(response, route)

    def down_for(self, endpoint: str) -> float:
        """Seconds until a failed endpoint is tried again (0: not known to be down)."""
        with self._lock:
            until, _ = self._down.get(endpoint, (0.0, 0.0))
        return max(0.0, until - self.clock())

    def _mark_down(self, endpoint: str) -> float:
        with self._lock:
            _, previous = self._down.get(endpoint, (0.0, 0.0))
            backoff = min(BREAKER_MAX_S, previous * 2) if previous else BREAKER_FIRST_S
            self._down[endpoint] = (self.clock() + backoff, backoff)
        return backoff

    def role_config(self, role: str) -> RoleConfig:
        """The role's mapping; `RoleNotConfigured` if there is none."""
        return self._role(role)

    def _role(self, role: str) -> RoleConfig:
        try:
            return self.config.roles[role]
        except KeyError:
            raise RoleNotConfigured(f"no model is configured for role {role!r}") from None


def model_key(role: RoleConfig, *, embed: bool = False) -> str:
    """What identifies the model's output for a role: the model name, and for embedding
    roles the vector size and the document prefix. A stage's results made under another
    key are recomputed."""
    if not embed:
        return role.model
    prefix = hashlib.sha256(role.document_prefix.encode()).hexdigest()[:8]
    return f"{role.model}|{role.dims or '?'}|{prefix}"


def check_llm_config(config: LlmConfig) -> None:
    """Startup checks beyond the schema. Remote endpoints arrive with a later module
    (`remote_llm`); until then they're refused, so nothing can be sent off the machine."""
    remote = sorted(n for n, ep in config.endpoints.items() if ep.trust == "remote")
    if remote:
        raise ValueError(
            f"llm.yaml: endpoint(s) {', '.join(remote)} have trust: remote; remote endpoints "
            "need the remote_llm module, which isn't available yet"
        )


def with_reasoning(request: ModelRequest, role_cfg: RoleConfig) -> ModelRequest:
    """A chat request with the role's `reasoning` setting as `reasoning_effort` (unless
    it's `default`, or the caller set it already)."""
    if (
        not isinstance(request, ChatRequest)
        or role_cfg.reasoning == "default"
        or "reasoning_effort" in request.options
    ):
        return request
    return dataclasses.replace(
        request, options={**request.options, "reasoning_effort": role_cfg.reasoning}
    )
