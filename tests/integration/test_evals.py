# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""`steward eval` with a fake model server: every built-in suite runs through its module's
real prompt, a model that knows the answers scores full marks and one that doesn't is
caught, results and baselines are kept (never the answers), and a model that goes away
leaves nothing recorded. The sets themselves are checked in `tests/unit/test_evals_helpers.py`."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from model_server import FakeModelServer, llm_config, model_server
from sqlalchemy import select
from support import eps, make_module, write_config
from typer.testing import CliRunner

from steward.cli import app as cli_app
from steward.core import backup, evals
from steward.core.config import load_config
from steward.core.db.schema import eval_runs
from steward.core.runtime import Runtime, build_runtime
from steward.modules.documents import DocumentsModule
from steward.modules.fields import FieldsModule
from steward.modules.photos import PhotosModule
from steward.modules.search import SearchModule
from steward.modules.tags import TagsModule
from steward.sdk import EvalCaseResult, EvalModel, Registry

MODULES = (DocumentsModule, SearchModule, FieldsModule, TagsModule, PhotosModule)
SOURCE = Path(__file__).parents[2] / "src" / "steward" / "modules"
CHAT = {"endpoint": "local", "model": "fake-chat"}


def load_set(path: str) -> dict[str, Any]:
    return dict(yaml.safe_load((SOURCE / path).read_text()))


class Oracle:
    """Answers from the sets' labels, finding the case in what it's sent."""

    def __init__(self) -> None:
        self.fields = load_set("fields/evals/fields.yaml")["cases"]
        self.tags = load_set("tags/evals/apply_tags.yaml")["cases"]
        self.captions = load_set("photos/evals/caption.yaml")["cases"]
        self.drawn = 0

    def __call__(self, body: dict[str, Any]) -> str:
        user = body["messages"][-1]["content"]
        if isinstance(user, list):
            case = self.captions[self.drawn % len(self.captions)]
            self.drawn += 1
            return " and ".join(group[0] for group in case["must"]) + "."
        system = str(body["messages"][1]["content"])
        if "pull out a few facts" in system:
            case = next(c for c in self.fields if f"File name: {c['file']}\n" in user)
            return json.dumps(case["expect"])
        case = next(c for c in self.tags if f"File name: {c['file']}\n" in user)
        return json.dumps({"tags": case.get("expect", [])})


class Env:
    def __init__(self, tmp_path: Path, server: FakeModelServer, *extra: type, **embed: Any) -> None:
        self.server = server
        llm = llm_config(server.url, **embed)
        llm["roles"].update(
            fields=CHAT, apply_tags=CHAT, caption={**CHAT, "capabilities": ["vision"]}
        )
        self.modules = (*MODULES, *extra)
        self.config_dir = write_config(
            tmp_path / "config",
            steward={"data_dir": str(tmp_path / "data")},
            modules={
                "modules": {
                    **{name: {"enabled": False} for name in ("files_ui", "shares")},
                    **{m.name: {"enabled": True} for m in self.modules},  # type: ignore[attr-defined]
                }
            },
            llm=llm,
        )
        self.runtime: Runtime = build_runtime(
            load_config(self.config_dir), entry_points=eps(*self.modules)
        )
        self.runtime.upgrade_databases()

    def cli(self, *args: str) -> Any:
        env = {"STEWARD_CONFIG_DIR": str(self.config_dir)}
        return CliRunner().invoke(cli_app, list(args), env=env)

    def rows(self) -> list[Any]:
        with self.runtime.dbs.state.read() as conn:
            return list(conn.execute(select(eval_runs).order_by(eval_runs.c.run_id)))


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    with model_server(dims=512) as server:
        environment = Env(tmp_path, server)
        yield environment
        environment.runtime.close()


def test_every_model_role_with_a_prompt_has_a_suite(env: Env) -> None:
    suites = env.runtime.registrations.eval_suites()
    assert set(suites) == {"apply_tags", "caption", "embed", "fields"}
    assert suites["fields"].prompt == "fields.v1"
    assert suites["apply_tags"].prompt == "apply.v1"
    assert suites["caption"].prompt == "caption.v1"
    assert all(s.module for s in suites.values())


@pytest.mark.parametrize(
    ("role", "scores"),
    [
        ("fields", {"accuracy": 1.0, "no_guess": 1.0}),
        ("apply_tags", {"f1": 1.0, "no_force": 1.0, "tagged": 1.0}),
        ("caption", {"concepts": 1.0, "described": 1.0, "no_invention": 1.0}),
    ],
)
def test_a_model_that_knows_the_answers_gets_full_marks(
    env: Env, role: str, scores: Mapping[str, float]
) -> None:
    env.server.default = Oracle()
    run = evals.run_suite(env.runtime, role)
    assert run.failed == []
    assert run.metrics == scores
    assert run.prompt


def test_the_real_prompt_and_the_image_reach_the_model(env: Env) -> None:
    env.server.default = Oracle()
    evals.run_suite(env.runtime, "caption")
    chats = env.server.chats()
    assert len(chats) == 12
    assert chats[0]["max_tokens"] == 512
    assert "You describe photos" in chats[0]["messages"][0]["content"]
    assert chats[0]["messages"][1]["content"][1]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )


def test_a_model_that_guesses_is_caught(env: Env) -> None:
    def always_the_same(body: dict[str, Any]) -> str:
        return json.dumps({"doc_type": "invoice", "amount": 5, "currency": "USD", "sender": "x"})

    env.server.default = always_the_same
    run = evals.run_suite(env.runtime, "fields")
    assert run.metrics["accuracy"] < 0.5
    assert run.metrics["no_guess"] < 0.8
    assert "note-nothing-stated" in run.failed


def test_a_model_that_tags_everything_is_caught(env: Env) -> None:
    vocabulary = [t["name"] for t in load_set("tags/evals/apply_tags.yaml")["vocabulary"]]
    env.server.default = lambda body: json.dumps({"tags": vocabulary[:5]})
    run = evals.run_suite(env.runtime, "apply_tags")
    assert run.metrics["no_force"] == 0.0
    assert run.metrics["f1"] < 0.5
    assert run.metrics["tagged"] == 1.0


def test_a_model_that_refuses_to_tag_is_caught(env: Env) -> None:
    env.server.default = lambda body: json.dumps({"tags": []})
    run = evals.run_suite(env.runtime, "apply_tags")
    assert run.metrics["tagged"] == 0.0
    assert run.metrics["no_force"] == 1.0


def test_answers_that_never_fit_the_schema_score_nothing(env: Env) -> None:
    env.server.default = "I can't help with that"
    run = evals.run_suite(env.runtime, "fields")
    assert run.metrics["accuracy"] == 0.0
    assert len(run.failed) == run.cases


def test_a_description_that_invents_people_is_caught(env: Env) -> None:
    env.server.default = "A man and a woman stand next to a dog."
    run = evals.run_suite(env.runtime, "caption")
    assert run.metrics["no_invention"] == 0.0
    assert run.metrics["concepts"] == 0.0


def test_embeddings_are_scored_with_the_query_prefix_and_the_floor(env: Env) -> None:
    env.runtime.close()
    run = evals.run_suite(env.runtime, "embed")
    assert set(run.metrics) == {"above_floor", "mrr", "recall_at_3", "rejects_unrelated"}
    assert 0 < run.metrics["mrr"] <= 1
    queries = [t for t in env.server.embedded() if t.startswith("who fixed")]
    assert queries == ["who fixed the leak under the sink"]


def test_queries_and_passages_get_their_own_prefixes(tmp_path: Path) -> None:
    with model_server(dims=512) as server:
        env = Env(
            tmp_path,
            server,
            query_prefix="find: ",
            document_prefix="passage: ",
            min_similarity=0.5,
        )
        evals.run_suite(env.runtime, "embed")
        env.runtime.close()
        sent = server.embedded()
    assert "find: who fixed the leak under the sink" in sent
    assert all(t.startswith(("find: ", "passage: ")) for t in sent)
    assert sum(t.startswith("passage: ") for t in sent) == 20


def test_results_are_kept_without_the_answers(env: Env) -> None:
    env.server.default = "SECRET-ANSWER-TEXT"
    run = evals.run_suite(env.runtime, "caption")
    rows = env.rows()
    assert [r.run_id for r in rows] == [run.run_id]
    row = rows[0]
    assert (row.role, row.model, row.prompt, row.cases) == (
        "caption",
        "fake-chat",
        "caption.v1",
        12,
    )
    assert "SECRET-ANSWER-TEXT" not in json.dumps([row.metrics, row.failed])
    exported = backup.export_state(env.runtime)["core"]["eval_runs"]
    assert [r["run_id"] for r in exported] == [run.run_id]


def test_a_model_going_away_ends_the_run_and_records_nothing(env: Env) -> None:
    env.server.status = 503
    result = env.cli("eval", "fields")
    assert result.exit_code == 1
    assert env.rows() == []


def test_a_role_without_a_model_or_a_set_says_so(tmp_path: Path) -> None:
    with model_server() as server:
        env = Env(tmp_path, server)
        env.runtime.close()
        assert "no eval set for role 'generate_tags'" in env.cli("eval", "generate_tags").output
        write_config(tmp_path / "config", llm={"endpoints": {}, "roles": {}})
        result = env.cli("eval", "fields")
        assert result.exit_code == 1
        assert "not mapped" in result.output


def test_the_command_compares_with_the_baseline_and_fails_on_a_regression(env: Env) -> None:
    env.server.default = Oracle()
    first = env.cli("eval", "apply_tags", "--baseline")
    assert first.exit_code == 0, first.output
    assert "saved as the baseline" in first.output

    env.server.default = Oracle()
    same = env.cli("eval", "apply_tags")
    assert same.exit_code == 0, same.output
    assert "baseline 1.000" in same.output

    env.server.default = lambda body: json.dumps({"tags": []})
    worse = env.cli("eval", "apply_tags", "--verbose")
    assert worse.exit_code == 1
    assert "WORSE" in worse.output
    assert "FAIL  hardware-invoice" in worse.output

    history = env.cli("eval", "apply_tags", "--history")
    assert history.output.count("fake-chat") == 3
    assert history.output.count("baseline") == 1
    assert sum(r.baseline for r in env.rows()) == 1

    listing = env.cli("eval").output
    assert "apply_tags (tags): fake-chat; baseline f1 1.000" in listing
    assert "fields (fields): fake-chat; baseline accuracy none" in listing


def test_a_new_baseline_replaces_the_old_one(env: Env) -> None:
    env.server.default = Oracle()
    env.cli("eval", "apply_tags", "--baseline")
    env.server.default = Oracle()
    env.cli("eval", "apply_tags", "--baseline")
    assert [r.baseline for r in env.rows()] == [False, True]


def test_a_changed_set_isnt_compared(env: Env) -> None:
    env.server.default = Oracle()
    run = evals.run_suite(env.runtime, "apply_tags")
    other = evals.EvalRecord(**{**vars_of(run), "set_hash": "other", "metrics": {"f1": 0.0}})
    assert all(c.baseline is None and not c.regressed for c in evals.compare(other, run))


def vars_of(record: evals.EvalRecord) -> dict[str, Any]:
    return {name: getattr(record, name) for name in record.__slots__}


def test_a_module_can_bring_its_own_suite(tmp_path: Path) -> None:
    cases = tmp_path / "mine.yaml"
    cases.write_text("words: [one, two]\n")

    def run(data: Mapping[str, Any], model: EvalModel) -> Iterator[EvalCaseResult]:
        for word in data["words"]:
            answer = model.chat([{"role": "user", "content": word}])
            yield EvalCaseResult(word, {"echo": 1.0 if answer == "ok" else 0.0}, answer == "ok")

    def register(r: Registry) -> None:
        r.model_role("mine", kind="chat", description="Answers")
        r.eval_suite("mine", run, cases=cases, primary="echo", prompt="mine.v1")

    with model_server() as server:
        module = make_module("mine", register=register)
        env = Env(tmp_path, server, module)
        result = env.runtime.registrations.eval_suites()["mine"]
        assert result.module == "mine"
        with pytest.raises(evals.EvalError, match="can't be used"):
            evals.run_suite(env.runtime, "mine")
        env.runtime.close()


def test_registering_a_suite_needs_its_cases_file(tmp_path: Path) -> None:
    def register(r: Registry) -> None:
        r.eval_suite("x", lambda d, m: [], cases=tmp_path / "missing.yaml", primary="a")

    module = make_module("broken", register=register)
    config_dir = write_config(
        tmp_path / "config",
        steward={"data_dir": str(tmp_path / "data")},
        modules={"modules": {"broken": {"enabled": True}}},
    )
    with pytest.raises(Exception, match="isn't a file"):
        build_runtime(load_config(config_dir), entry_points=eps(module))


def test_scores_never_quote_what_the_model_said(env: Env) -> None:
    env.server.default = "LEAKY-MODEL-OUTPUT"
    env.cli("eval", "caption", "--verbose")
    stored = json.dumps([list(r) for r in env.rows()], default=str)
    assert not re.search("LEAKY", stored)
