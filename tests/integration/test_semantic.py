# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Model roles, embeddings and hybrid search end to end, against a fake model server,
on two layouts: no model (graceful degradation), back-fill when one is mapped, a model
change (new space, old one dropped when the new one is complete), the server going
down (jobs wait without using attempts; search falls back to words), and the scope
rules for results found by meaning."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from api_support import log_in, make_owner
from fastapi.testclient import TestClient
from model_server import FakeModelServer, llm_config, model_server
from sqlalchemy import func, select, update
from support import make_runtime, use_fast_hashes

from steward.api import create_app
from steward.core import embeddings, reindex, stages
from steward.core.config import DiscoverySettings
from steward.core.db import utcnow
from steward.core.db.schema import chunks, embedding_spaces, files, jobs, stage_runs
from steward.core.db.schema import embeddings as embeddings_table
from steward.core.discovery import Scanner
from steward.core.folders import FolderRef, load_policy, store
from steward.core.runtime import Runtime
from steward.core.search import SearchResults, search_all
from steward.core.worker import Worker
from steward.modules.documents import DocumentsModule
from steward.modules.search import SearchModule

ACTOR = "user:1"
EMBED_STAGE = "search.embed"

LAYOUTS: dict[str, dict[str, str]] = {
    "numbered": {"docs": "10-documents", "private": "90-private", "media": "20-media"},
    "flat": {"docs": "Documents", "private": "Private", "media": "Photos"},
}
TYPES = {"docs": "documents", "private": "private", "media": "media"}


class Env:
    def __init__(
        self, tmp_path: Path, layout: str, server: FakeModelServer | None, **llm: Any
    ) -> None:
        self.tmp_path = tmp_path
        self.spec = LAYOUTS[layout]
        self.root = tmp_path / "lib"
        d, p = self.spec["docs"], self.spec["private"]
        self.files = {
            f"{d}/invoice.txt": "Invoice for faucet repair. Total due 400 dollars.",
            f"{d}/lease.txt": "Lease agreement, paid monthly.",
            f"{d}/recipe.txt": "Roast the potatoes with rosemary and olive oil.",
            f"{p}/hidden-invoice.txt": "Invoice for faucet work in the private folder.",
        }
        for relpath, text in self.files.items():
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.runtime = self.build(server, **llm)
        with self.runtime.dbs.state.write() as conn:
            store.upsert_library(conn, "lib", root=str(self.root), actor=ACTOR)
            for key, type_name in TYPES.items():
                (self.root / self.spec[key]).mkdir(exist_ok=True)
                store.set_type(conn, FolderRef("lib", self.spec[key]), type_name, actor=ACTOR)
            store.ensure_default_roles(conn, actor=ACTOR)
        self.scan()

    def build(self, server: FakeModelServer | None, **llm: Any) -> Runtime:
        files: dict[str, Any] = {}
        if server is not None:
            files["llm"] = llm_config(server.url, **llm)
        return make_runtime(self.tmp_path, (DocumentsModule, SearchModule), **files)

    def restart(self, server: FakeModelServer | None, **llm: Any) -> None:
        """A new runtime on the same data (as after editing llm.yaml), started like
        `steward worker`: a full pass when the stage graph changed."""
        self.runtime.close()
        self.runtime = self.build(server, **llm)
        self.drain()

    def drain(self) -> None:
        if stages.full_pass_needed(self.runtime.dbs, self.runtime.stages):
            with self.runtime.dbs.state.write() as conn:
                stages.enqueue_full_pass(conn)
        Worker(self.runtime).drain()

    def scan(self) -> None:
        Scanner(self.runtime.dbs, DiscoverySettings()).scan("lib")
        self.drain()

    def hash_of(self, relpath: str) -> str:
        with self.runtime.dbs.state.read() as conn:
            return str(
                conn.execute(
                    select(files.c.content_hash).where(files.c.relpath == relpath)
                ).scalar_one()
            )

    def run_of(self, relpath: str, stage: str = EMBED_STAGE) -> Any:
        with self.runtime.dbs.index.read() as conn:
            return conn.execute(
                select(stage_runs).where(
                    stage_runs.c.content_hash == self.hash_of(relpath), stage_runs.c.stage == stage
                )
            ).one_or_none()

    def spaces(self) -> list[embeddings.Space]:
        with self.runtime.dbs.index.read() as conn:
            return embeddings.spaces(conn)

    def search(self, query: str, *, scope: str = "normal", mode: str = "all") -> SearchResults:
        with self.runtime.dbs.state.read() as conn:
            policy = load_policy(conn)
        return search_all(
            self.runtime.dbs,
            policy,
            query,
            scope=scope,  # type: ignore[arg-type]
            mode=mode,  # type: ignore[arg-type]
            embed=self.runtime.query_embedder,
        )

    def names(self, query: str, **kwargs: Any) -> list[str]:
        return [h.name for h in self.search(query, **kwargs).hits]

    def health(self, name: str) -> tuple[str, str]:
        result = asyncio.run(self.runtime.registrations.health_checks()[name]())
        return result.status, result.detail


@pytest.fixture
def server() -> Iterator[FakeModelServer]:
    with model_server() as s:
        yield s


@pytest.fixture(params=sorted(LAYOUTS))
def layout(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def env(tmp_path: Path, layout: str, server: FakeModelServer) -> Iterator[Env]:
    e = Env(tmp_path, layout, server)
    yield e
    e.runtime.close()


# --- no model --------------------------------------------------------------------------


def test_without_a_model_search_works_by_words(tmp_path: Path, layout: str) -> None:
    env = Env(tmp_path, layout, None)
    try:
        assert EMBED_STAGE not in env.runtime.stages.stages
        assert "not mapped" in env.runtime.stages.inactive[EMBED_STAGE]
        assert env.run_of(f"{env.spec['docs']}/invoice.txt") is None
        status, detail = env.health("core.models")
        assert status == "warn"
        assert "'embed'" in detail
        assert "search.embed" in detail
        results = env.search("faucet")
        assert [h.name for h in results.hits] == ["invoice.txt"]
        assert results.semantic is None
        assert env.names("plumber bill") == []
        assert env.health("core.embeddings")[0] == "ok"
    finally:
        env.runtime.close()


# --- embedding and searching by meaning ------------------------------------------------


def test_chunks_are_embedded_and_found_by_meaning(env: Env, server: FakeModelServer) -> None:
    d = env.spec["docs"]
    run = env.run_of(f"{d}/invoice.txt")
    assert run.status == "done"
    assert run.model == env.runtime.stages.model_keys[EMBED_STAGE]
    [space] = env.spaces()
    assert space.status == "complete"
    assert (space.model, space.dims) == ("fake-embed", 32)
    with env.runtime.dbs.index.read() as conn:
        n_chunks = conn.execute(select(func.count()).select_from(chunks)).scalar_one()
        assert embeddings.count(conn, space.space_id) == n_chunks

    results = env.search("plumber bill")  # no word in common with the invoice
    assert results.semantic is True
    [hit] = results.hits
    assert hit.name == "invoice.txt"
    assert hit.semantic
    assert "".join(t for t, _ in hit.snippet).startswith("Invoice for faucet repair")
    assert not any(marked for _, marked in hit.snippet)
    assert env.names("rent") == ["lease.txt"]

    # a word match keeps its highlighted snippet and isn't "only by meaning"
    hit = env.search("invoice").hits[0]
    assert hit.name == "invoice.txt"
    assert not hit.semantic
    assert ("Invoice", True) in hit.snippet


def test_meaning_respects_scope_mode_and_threshold(env: Env, server: FakeModelServer) -> None:
    assert "hidden-invoice.txt" not in env.names("plumber bill")
    assert env.names("plumber bill", scope="locked") == ["hidden-invoice.txt"]
    assert env.names("plumber bill", mode="text") == ["invoice.txt"]
    assert env.names("plumber bill", mode="names") == []
    assert env.search("plumber bill", mode="names").semantic is None
    # nothing is similar enough to unrelated words
    assert env.names("zebra galaxy") == []


def test_query_embedding_uses_the_prefix_and_is_cached(tmp_path: Path, layout: str) -> None:
    with model_server() as server:
        env = Env(tmp_path, layout, server, query_prefix="query: ", document_prefix="passage: ")
        try:
            assert all(t.startswith("passage: ") for t in server.embedded())
            before = len(server.requests)
            env.search("plumber bill")
            env.search("plumber   bill")
            queries = [t for t in server.embedded()[-2:] if t.startswith("query: ")]
            assert queries == ["query: plumber bill"]
            assert len(server.requests) == before + 1
        finally:
            env.runtime.close()


# --- the model server failing ----------------------------------------------------------


def test_search_falls_back_to_words_when_the_model_is_down(
    env: Env, server: FakeModelServer
) -> None:
    server.status = 500
    results = env.search("faucet repair")
    assert results.semantic is False
    assert [h.name for h in results.hits] == ["invoice.txt"]
    sent = len(server.requests)
    env.search("lease")  # the endpoint is known to be down: no request at all
    assert len(server.requests) == sent


def test_stage_jobs_wait_for_the_model_without_using_attempts(tmp_path: Path, layout: str) -> None:
    with model_server() as server:
        server.status = 503
        env = Env(tmp_path, layout, server)
        try:
            d = env.spec["docs"]
            assert env.run_of(f"{d}/invoice.txt") is None  # nothing recorded as failed
            with env.runtime.dbs.state.read() as conn:
                waiting = conn.execute(
                    select(jobs).where(jobs.c.type == f"core.stage.{EMBED_STAGE}")
                ).all()
            assert waiting
            assert all(j.status == "queued" and j.attempts == 0 for j in waiting)
            assert all("waiting for the model" in (j.last_error or "") for j in waiting)
            assert all(j.run_after > utcnow() + timedelta(seconds=10) for j in waiting)
            assert not env.spaces()

            # The server comes back; once the jobs are due they run.
            server.status = None
            env.runtime.gateway._down.clear()
            with env.runtime.dbs.state.write() as conn:
                conn.execute(update(jobs).values(run_after=utcnow()))
            env.drain()
            assert env.run_of(f"{d}/invoice.txt").status == "done"
            assert env.names("plumber bill") == ["invoice.txt"]
        finally:
            env.runtime.close()


# --- adding and changing the model ------------------------------------------------------


def test_mapping_a_model_back_fills(tmp_path: Path, layout: str) -> None:
    env = Env(tmp_path, layout, None)
    try:
        d = env.spec["docs"]
        assert env.run_of(f"{d}/invoice.txt") is None
        with model_server() as server:
            env.restart(server)
            assert env.run_of(f"{d}/invoice.txt").status == "done"
            assert env.names("plumber bill") == ["invoice.txt"]
            assert env.spaces()[0].status == "complete"
            assert env.health("core.models")[0] == "ok"
            status, detail = env.health("core.embeddings")
            assert status == "ok"
            assert "complete" in detail
    finally:
        env.runtime.close()


def test_changing_the_model_fills_a_new_space_and_drops_the_old(
    env: Env, server: FakeModelServer
) -> None:
    d = env.spec["docs"]
    [old] = env.spaces()
    old_key = env.run_of(f"{d}/invoice.txt").model
    env.restart(server, embed_model="fake-embed-2")
    [new] = env.spaces()
    assert new.space_id != old.space_id
    assert (new.model, new.status) == ("fake-embed-2", "complete")
    assert env.run_of(f"{d}/invoice.txt").model != old_key
    assert env.names("plumber bill") == ["invoice.txt"]
    # The old space stays while the new one fills.
    with env.runtime.dbs.index.write() as conn:
        conn.execute(
            update(embedding_spaces)
            .where(embedding_spaces.c.space_id == new.space_id)
            .values(status="filling")
        )
        conn.execute(
            embedding_spaces.insert().values(
                role="embed",
                endpoint="local",
                model="fake-embed",
                dims=32,
                model_key=old_key.split("=", 1)[1],
                status="complete",
                created_at=utcnow(),
            )
        )
    with env.runtime.dbs.state.write() as conn:
        stages.enqueue_full_pass(conn)  # pending work: the new space isn't complete
    stages.complete_spaces(env.runtime.dbs, env.runtime.stages)
    assert len(env.spaces()) == 2
    Worker(env.runtime).drain()
    assert [(s.model, s.status) for s in env.spaces()] == [("fake-embed-2", "complete")]


def test_changing_the_dims_or_prefix_counts_as_a_new_model(tmp_path: Path, layout: str) -> None:
    with model_server(dims=24) as server:
        env = Env(tmp_path, layout, server, dims=24)
        try:
            key = env.runtime.stages.model_keys[EMBED_STAGE]
            env.restart(server, dims=24, document_prefix="passage: ")
            assert env.runtime.stages.model_keys[EMBED_STAGE] != key
            [space] = env.spaces()
            assert space.dims == 24
        finally:
            env.runtime.close()


def test_a_dims_mismatch_fails_the_stage(tmp_path: Path, layout: str) -> None:
    with model_server(dims=16) as server:
        env = Env(tmp_path, layout, server)  # llm.yaml says 32
        try:
            run = env.run_of(f"{env.spec['docs']}/invoice.txt")
            assert run.status == "failed"
            assert "dims 32" in run.error
            assert not env.spaces()
        finally:
            env.runtime.close()


# --- storage -----------------------------------------------------------------------------


def test_vectors_go_with_their_chunks(env: Env) -> None:
    content_hash = env.hash_of(f"{env.spec['docs']}/recipe.txt")
    with env.runtime.dbs.index.write() as conn:
        ids = list(
            conn.execute(
                select(chunks.c.chunk_id).where(chunks.c.content_hash == content_hash)
            ).scalars()
        )
        assert ids
        conn.execute(chunks.delete().where(chunks.c.content_hash == content_hash))
        left = conn.execute(
            select(func.count())
            .select_from(embeddings_table)
            .where(embeddings_table.c.chunk_id.in_(ids))
        ).scalar_one()
    assert left == 0


def test_vectors_are_normalized_and_validated() -> None:
    blob = embeddings.pack([3.0, 4.0])
    assert embeddings.unpack(blob) == pytest.approx([0.6, 0.8])
    for bad in ([], [0.0, 0.0], [float("nan"), 1.0], [float("inf")]):
        with pytest.raises(embeddings.EmbeddingError):
            embeddings.pack(bad)


# --- web ----------------------------------------------------------------------------------


def test_search_pages_mark_results_found_by_meaning(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fast_hashes(monkeypatch)
    make_owner(env.runtime)
    client = TestClient(create_app(env.runtime), follow_redirects=False)
    log_in(client)
    page = client.get("/ui/search", params={"q": "plumber bill"})
    assert page.status_code == 200
    assert "invoice.txt" in page.text
    assert '<span class="related">related</span>' in page.text
    assert "being rebuilt" not in page.text
    data = client.get("/api/v1/search", params={"q": "plumber bill"}).json()
    assert data["semantic"] is True
    assert data["rebuilding"] is False
    assert [(h["name"], h["semantic"]) for h in data["hits"]] == [("invoice.txt", True)]
    with env.runtime.dbs.state.write() as conn:
        reindex.request(conn, actor="test")
    assert "being rebuilt" in client.get("/ui/search", params={"q": "plumber bill"}).text
    assert client.get("/api/v1/search", params={"q": "bill"}).json()["rebuilding"] is True
