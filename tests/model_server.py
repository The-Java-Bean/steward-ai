# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""A fake OpenAI-compatible model server for tests (no real model is ever called).

Embeddings are bags of hashed words: texts sharing words (or words listed as
`SYNONYMS` of each other) get similar vectors, so tests can find a document "by
meaning" with a query that shares none of its words. Chat answers come from a script
(`replies`), defaulting to "ok". Every request is recorded.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DIMS = 32
SYNONYMS = {
    "plumber": "faucet",
    "plumbing": "faucet",
    "bill": "invoice",
    "rent": "lease",
    "landlord": "lease",
    "tenant": "lease",
}
"""Word -> the word it embeds as."""

_WORD = re.compile(r"[a-z0-9]+")

type Reply = str | Callable[[dict[str, Any]], str | dict[str, Any]]


def embed_text(text: str, dims: int = DIMS) -> list[float]:
    vector = [0.0] * dims
    for word in _WORD.findall(text.lower()):
        word = SYNONYMS.get(word, word)
        bucket = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big") % dims
        vector[bucket] += 1.0
    if not any(vector):
        vector[0] = 1.0
    return vector


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))


class FakeModelServer:
    def __init__(self, dims: int = DIMS) -> None:
        self.dims = dims
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.replies: list[Reply] = []
        self.default: Reply = "ok"
        """The answer once `replies` runs out."""
        self.status: int | None = None
        """Answer every request with this HTTP status instead (e.g. 500)."""
        self._server: ThreadingHTTPServer | None = None
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def embedded(self) -> list[str]:
        return [
            t for path, body in self.requests if path.endswith("/embeddings") for t in body["input"]
        ]

    def chats(self) -> list[dict[str, Any]]:
        return [body for path, body in self.requests if path.endswith("/chat/completions")]

    def start(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                with owner._lock:
                    owner.requests.append((self.path, body))
                if owner.status is not None:
                    self._send(owner.status, {"error": "fake failure"})
                    return
                if self.path.endswith("/embeddings"):
                    data = [
                        {"object": "embedding", "index": i, "embedding": embed_text(t, owner.dims)}
                        for i, t in enumerate(body["input"])
                    ]
                    self._send(200, {"object": "list", "data": data})
                elif self.path.endswith("/chat/completions"):
                    with owner._lock:
                        reply = owner.replies.pop(0) if owner.replies else owner.default
                    answer = reply(body) if callable(reply) else reply
                    message = answer if isinstance(answer, dict) else {"content": answer}
                    self._send(
                        200,
                        {"choices": [{"index": 0, "message": {"role": "assistant", **message}}]},
                    )
                else:
                    self._send(404, {"error": "not found"})

            def _send(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@contextmanager
def model_server(dims: int = DIMS) -> Iterator[FakeModelServer]:
    server = FakeModelServer(dims)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def llm_config(
    url: str, *, embed_model: str = "fake-embed", dims: int | None = DIMS, **embed: Any
) -> dict[str, Any]:
    """An `llm.yaml` mapping `embed` (and `chat`) to the fake server."""
    role: dict[str, Any] = {"endpoint": "local", "model": embed_model, **embed}
    if dims is not None:
        role["dims"] = dims
    return {
        "endpoints": {"local": {"base_url": url, "trust": "local", "timeout_s": 5}},
        "roles": {"embed": role, "chat": {"endpoint": "local", "model": "fake-chat"}},
    }
