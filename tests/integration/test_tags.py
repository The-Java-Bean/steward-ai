# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Fields and tags end to end with a fake model server, on two layouts: fields read from
documents into properties (and filtered on in search), the vocabulary (created, renamed
with aliases, merged, deprecated, each undoable), tags chosen by the model only from the
vocabulary, folder types' default tags, your additions and removals winning over both,
search filters and counts that respect locked folders, skipped files tagged again when
tags are added, the pages and the CLI, and the bootstrap (never sampling locked files)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from api_support import form_token, log_in, make_owner
from doc_fixtures import make_pdf
from fastapi.testclient import TestClient
from model_server import FakeModelServer, llm_config, model_server
from sqlalchemy import select
from support import eps, write_config
from typer.testing import CliRunner

from steward.api import create_app
from steward.cli import app as cli_app
from steward.core import content, stages
from steward.core.config import DiscoverySettings, load_config
from steward.core.db.schema import files, stage_runs
from steward.core.discovery import Scanner
from steward.core.folders import FolderRef, TypeDefinition, load_policy, store
from steward.core.runtime import Runtime, build_runtime
from steward.core.search import SearchFilters, search_all, tag_counts
from steward.core.tags import bootstrap, effective
from steward.core.tags import io as tag_io
from steward.core.tags import store as tag_store
from steward.core.worker import Worker
from steward.modules.documents import DocumentsModule
from steward.modules.fields import FieldsModule
from steward.modules.files_ui import FilesUiModule
from steward.modules.search import SearchModule
from steward.modules.tags import TagsModule

ACTOR = "user:1"
TAG_STAGE = "tags.apply"
FIELDS_STAGE = "fields.extract"

LAYOUTS: dict[str, dict[str, str]] = {
    "numbered": {"docs": "10-docs", "receipts": "10-docs/receipts", "private": "90-private"},
    "flat": {"docs": "Documents", "receipts": "Receipts", "private": "Private"},
}

TEXTS = {
    "invoice": "INVOICE 2024-117 from Acme Plumbing. Date 2024-03-05. Total due 120.50 USD.",
    "letter": "Dear tenant, the rent for the flat changes next month. Your landlord.",
    "receipt": "RECEIPT Corner Shop. 2024-04-02. Total 9.99 EUR. Thank you.",
    "secret": "INVOICE from a doctor about a private matter. Total 300 USD.",
}
FIELDS = {
    "invoice": {
        "doc_type": "Invoice",
        "title": "Acme Plumbing invoice",
        "date": "2024-03-05",
        "sender": "Acme Plumbing",
        "amount": 120.5,
        "currency": "$",
    },
    "letter": {"doc_type": "letter", "title": "Rent change", "date": None, "sender": "Landlord"},
    "receipt": {"doc_type": "receipt", "amount": 9.99, "currency": "EUR", "vendor": "Corner Shop"},
    "secret": {"doc_type": "invoice", "amount": 300, "currency": "USD"},
}
CHOICES = {
    "invoice": ["finance/invoice", "home"],
    "letter": ["home/lease"],
    "receipt": [],
    "secret": ["finance/invoice"],
}


def _which(text: str) -> str:
    for key, body in TEXTS.items():
        if body[:30] in text:
            return key
    return ""


def fake_model(body: dict[str, Any]) -> str:
    """Answers by role (each mapped to its own model name) and by the document."""
    system = "\n".join(m["content"] for m in body["messages"] if m["role"] == "system")
    user = body["messages"][-1]["content"]
    if body["model"] == "fake-fields":
        return json.dumps(FIELDS.get(_which(user), {}))
    if body["model"] == "fake-tags":
        offered = system.split("The tags:\n", 1)[1]
        chosen = [n for n in CHOICES.get(_which(user), []) if f"- {n}" in offered]
        return json.dumps({"tags": chosen})
    if body["model"] == "fake-gen":
        if "Merge them" in system:
            return json.dumps({"tags": [{"name": "Finance/Invoice", "description": "Bills"}]})
        return json.dumps(
            {
                "tags": [
                    {"name": "finance/invoice", "description": "Bills to pay"},
                    {"name": "home/lease", "description": "Renting"},
                    {"name": "bad:name", "description": "dropped"},
                ]
            }
        )
    return "ok"


class Env:
    def __init__(self, tmp_path: Path, layout: str, server: FakeModelServer) -> None:
        self.tmp_path = tmp_path
        self.server = server
        self.spec = LAYOUTS[layout]
        self.root = tmp_path / "lib"
        d, r, p = self.spec["docs"], self.spec["receipts"], self.spec["private"]
        self.paths = {
            "invoice": f"{d}/invoice.pdf",
            "letter": f"{d}/letter.pdf",
            "receipt": f"{r}/receipt.txt",
            "secret": f"{p}/secret.pdf",
            "blank": f"{d}/blank.pdf",
        }
        for key, relpath in self.paths.items():
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            if key == "receipt":
                path.write_text(TEXTS[key])
            else:
                path.write_bytes(make_pdf([TEXTS.get(key, "")]))
        self.config_dir = self.write_config()
        self.runtime = self.build()
        with self.runtime.dbs.state.write() as conn:
            store.upsert_library(conn, "lib", root=str(self.root), actor=ACTOR)
            store.define_type(
                conn,
                "receipts",
                TypeDefinition(extends="documents", fields=("vendor",)),
                actor=ACTOR,
            )
            store.set_type(conn, FolderRef("lib", d), "documents", actor=ACTOR)
            store.set_type(conn, FolderRef("lib", r), "receipts", actor=ACTOR)
            store.set_type(conn, FolderRef("lib", p), "private", actor=ACTOR)
            store.ensure_default_roles(conn, actor=ACTOR)
        Scanner(self.runtime.dbs, DiscoverySettings()).scan("lib")
        self.drain()

    def add_rule(self) -> None:
        """Give the receipts folder type a default tag."""
        with self.runtime.dbs.state.write() as conn:
            store.define_type(
                conn,
                "receipts",
                TypeDefinition(
                    extends="documents", default_tags=("finance/receipt",), fields=("vendor",)
                ),
                actor=ACTOR,
            )
        self.drain()

    def write_config(self) -> Path:
        llm = llm_config(self.server.url)
        for role, model in (
            ("fields", "fake-fields"),
            ("apply_tags", "fake-tags"),
            ("generate_tags", "fake-gen"),
        ):
            llm["roles"][role] = {"endpoint": "local", "model": model}
        modules = (DocumentsModule, SearchModule, FieldsModule, TagsModule, FilesUiModule)
        return write_config(
            self.tmp_path / "config",
            steward={"data_dir": str(self.tmp_path / "data")},
            modules={
                "modules": {
                    **{m.name: {"enabled": True} for m in modules},
                    "tags": {"enabled": True, "settings": {"min_vocabulary": 2}},
                    "photos": {"enabled": False},  # so the CLI loads the same modules
                    "shares": {"enabled": False},
                }
            },
            llm=llm,
        )

    def build(self) -> Runtime:
        modules = (DocumentsModule, SearchModule, FieldsModule, TagsModule, FilesUiModule)
        runtime = build_runtime(load_config(self.config_dir), entry_points=eps(*modules))
        runtime.upgrade_databases()
        return runtime

    def drain(self) -> None:
        if stages.full_pass_needed(self.runtime.dbs, self.runtime.stages):
            with self.runtime.dbs.state.write() as conn:
                stages.enqueue_full_pass(conn)
        Worker(self.runtime).drain()

    def path(self, key: str) -> str:
        return f"lib:{self.paths[key]}"

    def file(self, key: str) -> Any:
        with self.runtime.dbs.state.read() as conn:
            return conn.execute(select(files).where(files.c.relpath == self.paths[key])).one()

    def run_of(self, key: str, stage: str) -> Any:
        with self.runtime.dbs.index.read() as conn:
            return conn.execute(
                select(stage_runs).where(
                    stage_runs.c.content_hash == self.file(key).content_hash,
                    stage_runs.c.stage == stage,
                )
            ).one_or_none()

    def fields(self, key: str) -> dict[str, Any]:
        with self.runtime.dbs.index.read() as conn:
            return content.read_properties(conn, self.file(key).content_hash, "fields")

    def tags(self, key: str) -> dict[str, str]:
        """Effective tag -> source."""
        row = self.file(key)
        with self.runtime.dbs.state.read() as state, self.runtime.dbs.index.read() as index:
            found = effective.file_tags(
                state,
                index,
                load_policy(state),
                file_id=row.file_id,
                content_hash=row.content_hash,
                library_id="lib",
                relpath=row.relpath,
            )
        return {t.name: t.source for t in found}

    def search(self, query: str = "", *, scope: str = "normal", **filters: Any) -> list[str]:
        with self.runtime.dbs.state.read() as conn:
            policy = load_policy(conn)
        results = search_all(
            self.runtime.dbs,
            policy,
            query,
            filters=SearchFilters(**filters),
            scope=scope,  # type: ignore[arg-type]
            mode="names" if not query.replace("tag:", "").strip() else "all",
        )
        return sorted(h.name for h in results.hits)

    def run(self, name: str, params: dict[str, Any]) -> Any:
        entry = self.runtime.executor.run(name, params, actor=ACTOR, confirmed=True)
        return entry

    def undo(self, entry: Any) -> None:
        self.runtime.executor.undo(entry.entry_id, actor=ACTOR, confirmed=True)

    def vocabulary(self) -> dict[str, Any]:
        with self.runtime.dbs.state.read() as conn:
            return {t.name: t for t in tag_store.vocabulary(conn)}

    def import_tags(self, *names: str) -> None:
        text = "tags:\n" + "".join(f"  - name: {n}\n" for n in names)
        with self.runtime.dbs.state.write() as conn:
            tag_io.import_tags(conn, tag_io.parse(text), actor=ACTOR)


@pytest.fixture
def server() -> Iterator[FakeModelServer]:
    with model_server() as s:
        s.replies = [fake_model] * 200
        yield s


@pytest.fixture(params=sorted(LAYOUTS))
def layout(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def env(tmp_path: Path, layout: str, server: FakeModelServer) -> Iterator[Env]:
    e = Env(tmp_path, layout, server)
    yield e
    e.runtime.close()


# --- fields ------------------------------------------------------------------------


def test_fields_are_read_from_documents_and_cleaned(env: Env) -> None:
    assert env.fields("invoice") == {
        "doc_type": "invoice",
        "title": "Acme Plumbing invoice",
        "date": "2024-03-05",
        "sender": "Acme Plumbing",
        "amount": 120.5,
        "currency": "USD",
    }
    assert env.fields("letter") == {
        "doc_type": "letter",
        "title": "Rent change",
        "sender": "Landlord",
    }
    # A text file gets fields only where its folder type asks for some, with the
    # type's own fields in the schema.
    assert env.fields("receipt")["vendor"] == "Corner Shop"
    schema = next(
        c for c in env.server.chats() if c["model"] == "fake-fields" and "Corner Shop" in str(c)
    )
    assert "vendor" in json.dumps(schema)
    assert env.run_of("blank", FIELDS_STAGE).status == "skipped"


def test_search_filters_on_fields(env: Env) -> None:
    assert env.search("doc_type:invoice") == ["invoice.pdf"]  # locked one left out
    assert env.search("amount>100") == ["invoice.pdf"]
    assert env.search("amount<=10") == ["receipt.txt"]
    assert env.search('sender:"acme"') == ["invoice.pdf"]
    assert env.search("doc_type:invoice", scope="locked") == ["secret.pdf"]
    # Unknown keys aren't filters: they're words.
    assert env.search("nosuchkey:invoice") == []


# --- tags ------------------------------------------------------------------------


def test_without_tags_the_stage_skips_and_runs_again_when_tags_are_added(env: Env) -> None:
    assert env.run_of("invoice", TAG_STAGE).status == "skipped"
    assert env.run_of("invoice", TAG_STAGE).error == "no tags yet"
    assert not any(c["model"] == "fake-tags" for c in env.server.chats())
    # A folder type's default tag is created with the type and applies to its files;
    # one tag isn't enough for the model yet (`min_vocabulary` 2 here).
    env.add_rule()
    assert env.tags("receipt") == {"finance/receipt": "rule"}
    assert env.run_of("invoice", TAG_STAGE).error == "fewer than 2 tags yet"
    assert not any(c["model"] == "fake-tags" for c in env.server.chats())
    env.import_tags("finance/invoice", "home/lease", "home")
    env.drain()
    assert env.tags("invoice") == {"finance/invoice": "ai", "home": "ai"}
    assert env.tags("letter") == {"home/lease": "ai"}
    assert env.tags("secret") == {"finance/invoice": "ai"}
    # The model is only ever offered the vocabulary, and only picks from it.
    asked = [c for c in env.server.chats() if c["model"] == "fake-tags"]
    assert asked
    assert all("enum" in json.dumps(c) or "const" in json.dumps(c) for c in asked)


def test_your_decisions_win_over_ai_and_rules_and_undo(env: Env) -> None:
    env.add_rule()
    env.import_tags("finance/invoice", "home")
    env.drain()
    removed = env.run("tag.remove", {"path": env.path("invoice"), "tag": "home"})
    assert env.tags("invoice") == {"finance/invoice": "ai"}
    env.run("tag.remove", {"path": env.path("receipt"), "tag": "finance/receipt"})
    assert env.tags("receipt") == {}
    added = env.run("tag.add", {"path": env.path("letter"), "tag": "Personal/Letters"})
    assert env.tags("letter") == {"Personal/Letters": "user"}  # spelled as typed
    assert "Personal/Letters" in env.vocabulary()
    # Undoing the addition also removes the tag it created.
    env.undo(added)
    assert "Personal/Letters" not in env.vocabulary()
    assert env.tags("letter") == {}
    env.undo(removed)
    assert env.tags("invoice") == {"finance/invoice": "ai", "home": "ai"}
    env.run("tag.add", {"path": env.path("invoice"), "tag": "home"})  # the AI's, now yours
    assert env.tags("invoice")["home"] == "user"
    with pytest.raises(Exception, match="already"):
        env.run("tag.add", {"path": env.path("invoice"), "tag": "home"})
    with pytest.raises(Exception, match="no tag"):
        env.run("tag.remove", {"path": env.path("invoice"), "tag": "nope"})


def test_tag_filters_follow_hierarchy_aliases_and_scope(env: Env) -> None:
    env.add_rule()
    env.import_tags("finance/invoice", "home/lease", "home")
    env.drain()
    assert env.search(tags=("finance",)) == ["invoice.pdf", "receipt.txt"]
    assert env.search("tag:finance/invoice") == ["invoice.pdf"]
    assert env.search('tag:"home"') == ["invoice.pdf", "letter.pdf"]
    assert env.search("tag:finance", scope="locked") == ["secret.pdf"]
    with env.runtime.dbs.state.read() as conn:
        policy = load_policy(conn)
    counts = tag_counts(env.runtime.dbs, policy)
    assert counts["finance/invoice"] == 1  # the locked file isn't counted
    assert counts["finance/receipt"] == 1
    assert tag_counts(env.runtime.dbs, policy, scope="locked")["finance/invoice"] == 1

    rename = env.run("tag.update", {"tag": "home", "new_name": "house"})
    found = env.vocabulary()
    assert "house/lease" in found
    assert "home/lease" not in found
    assert found["house/lease"].aliases == ("home/lease",)
    assert env.search("tag:home/lease") == ["letter.pdf"]  # old names still work
    assert env.tags("letter") == {"house/lease": "ai"}
    env.undo(rename)
    assert "home/lease" in env.vocabulary()
    assert not env.vocabulary()["home/lease"].aliases


def test_merge_moves_decisions_and_undo_separates(env: Env) -> None:
    env.import_tags("finance/invoice", "home/lease", "home")
    env.drain()
    env.run("tag.add", {"path": env.path("letter"), "tag": "home"})
    env.run("tag.remove", {"path": env.path("invoice"), "tag": "home"})
    merge = env.run("tag.merge", {"tag": "home/lease", "into": "finance/invoice"})
    assert "home/lease" not in env.vocabulary()
    assert "home/lease" in env.vocabulary()["finance/invoice"].aliases
    assert env.tags("letter") == {"finance/invoice": "ai", "home": "user"}
    with pytest.raises(Exception, match="into itself"):
        env.run("tag.merge", {"tag": "home", "into": "home"})
    env.undo(merge)
    assert env.tags("letter") == {"home/lease": "ai", "home": "user"}
    assert not env.vocabulary()["finance/invoice"].aliases


def test_deprecated_tags_stay_on_files_but_are_not_offered(env: Env) -> None:
    env.import_tags("finance/invoice", "home", "travel")
    env.drain()
    env.run("tag.update", {"tag": "home", "status": "deprecated"})
    assert env.vocabulary()["home"].status == "deprecated"
    before = len(env.server.chats())
    stages.rerun(env.runtime.dbs, TAG_STAGE, statuses=None)
    env.drain()
    offered = [
        c["messages"][0]["content"]
        for c in env.server.chats()[before:]
        if c["model"] == "fake-tags"
    ]
    assert offered
    assert all("- home\n" not in o and not o.endswith("- home") for o in offered)


def test_export_import_round_trip(env: Env) -> None:
    env.import_tags("finance/invoice", "home")
    with env.runtime.dbs.state.write() as conn:
        tag_store.add_alias(conn, tag_store.require(conn, "home").tag_id, "house", actor=ACTOR)
        text = tag_io.export_tags(conn)
        parsed = tag_io.parse(text)
    assert {e.name for e in parsed.tags} >= {"finance/invoice", "home"}
    assert next(e for e in parsed.tags if e.name == "home").aliases == ["house"]
    with env.runtime.dbs.state.write() as conn:
        again = tag_io.import_tags(conn, parsed, actor=ACTOR)
    assert again.created == []
    assert again.aliases == []
    with pytest.raises(Exception, match="YAML|tags"):
        tag_io.parse("tags: [{nme: x}]")


# --- bootstrap ------------------------------------------------------------------------


def test_bootstrap_suggests_tags_and_never_samples_locked_files(env: Env) -> None:
    with env.runtime.dbs.state.read() as conn:
        policy = load_policy(conn)
    found = bootstrap.suggest(env.runtime.dbs, policy, env.runtime.gateway, samples=10)
    assert {e.name for e in found.tags} == {"finance/invoice", "home/lease"}
    asked = [c for c in env.server.chats() if c["model"] == "fake-gen"]
    assert asked
    assert all("private matter" not in json.dumps(c) for c in asked)
    assert any("Acme Plumbing" in json.dumps(c) for c in asked)


# --- pages and CLI ----------------------------------------------------------------


def test_tags_page_and_file_page(env: Env) -> None:
    make_owner(env.runtime)
    env.import_tags("finance/invoice", "home")
    env.drain()
    with TestClient(create_app(env.runtime), follow_redirects=False) as client:
        log_in(client)
        page = client.get("/ui/tags")
        assert page.status_code == 200
        assert "finance/receipt" in page.text or "receipt" in page.text
        token = form_token(page.text)
        made = client.post(
            "/ui/tags/create",
            data={"name": "travel", "description": "Trips", "csrf_token": token},
            follow_redirects=False,
        )
        assert made.status_code == 303
        assert "travel" in env.vocabulary()
        after = client.get(made.headers["location"])
        assert "Undo" in after.text
        undo = client.post(
            "/ui/tags/undo",
            data={"entry": made.headers["location"].rsplit("undo=", 1)[1], "csrf_token": token},
            follow_redirects=False,
        )
        assert undo.status_code == 303
        assert "travel" not in env.vocabulary()
        bad = client.post("/ui/tags/create", data={"name": "a:b", "csrf_token": token})
        assert bad.status_code == 409

        view = client.get("/ui/files/view", params={"path": env.path("invoice")})
        assert view.status_code == 200
        assert "finance/invoice" in view.text
        assert "/ui/search?tag=finance%2Finvoice" in view.text
        token = form_token(view.text)
        added = client.post(
            "/ui/files/tag",
            data={"path": env.path("invoice"), "tag": "travel", "csrf_token": token},
            follow_redirects=False,
        )
        assert added.status_code == 303
        assert env.tags("invoice")["travel"] == "user"
        client.post(
            "/ui/files/untag",
            data={"path": env.path("invoice"), "tag": "home", "csrf_token": token},
        )
        assert "home" not in env.tags("invoice")
        search = client.get("/ui/search", params={"tag": "travel"})
        assert "invoice.pdf" in search.text
        api = client.get("/api/v1/search", params={"q": "", "tag": "travel", "in": "names"})
        assert [h["name"] for h in api.json()["hits"]] == ["invoice.pdf"]


def test_cli(env: Env, tmp_path: Path) -> None:
    runner = CliRunner()
    base = ["--config-dir", str(env.config_dir)]
    env.runtime.close()
    yaml_file = tmp_path / "tags.yaml"
    yaml_file.write_text(
        "tags:\n  - name: finance/invoice\n    description: Bills\n    aliases: [bills]\n"
    )
    result = runner.invoke(cli_app, [*base, "tags", "import", str(yaml_file)])
    assert result.exit_code == 0, result.output
    assert "1 created" in result.output
    result = runner.invoke(cli_app, [*base, "tags", "add", env.path("letter"), "bills"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli_app, [*base, "tags", "list"])
    assert "finance/invoice  1" in result.output
    assert "also bills" in result.output
    result = runner.invoke(cli_app, [*base, "files", "info", env.path("letter")])
    assert "finance/invoice (user)" in result.output
    result = runner.invoke(cli_app, [*base, "search", "--tag", "finance", "--in", "names", ""])
    assert "letter.pdf" in result.output
    result = runner.invoke(cli_app, [*base, "tags", "export"])
    assert "name: finance/invoice" in result.output
    out = tmp_path / "boot.yaml"
    result = runner.invoke(cli_app, [*base, "tags", "bootstrap", "--output", str(out)])
    assert result.exit_code == 0, result.output
    assert "finance/invoice" in out.read_text()
    result = runner.invoke(cli_app, [*base, "tags", "retag", "--all"])
    assert result.exit_code == 0, result.output
    env.runtime = env.build()
