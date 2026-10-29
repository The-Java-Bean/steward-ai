# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Tag names, query filters, the fields module's cleaning and the tags module's schema
and tree, without databases or models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from steward.core.search import PropertyFilter, parse_query
from steward.core.tags.io import parse
from steward.core.tags.names import TagError, is_within, key, normalize, parent
from steward.modules.fields import extract
from steward.modules.tags import apply
from steward.modules.tags.routes import tree
from steward.sdk import TagInfo


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("finance/receipt", "finance/receipt"),
        ("  /Finance /  Receipt  copies/ ", "Finance/Receipt copies"),
        ("café", "café"),
    ],
)
def test_normalize(raw: str, stored: str) -> None:
    assert normalize(raw) == stored


@pytest.mark.parametrize(
    "bad", ["", "/", "a//b", "a:b", 'a"b', "a,b", "a\\b", "a\x00b", "/".join("abcdefg"), "x" * 65]
)
def test_bad_names(bad: str) -> None:
    with pytest.raises(TagError):
        normalize(bad)


def test_keys_and_hierarchy() -> None:
    assert key("Finance/Receipt") == key("finance/RECEIPT")
    assert is_within("finance/receipt", "finance")
    assert is_within("finance", "finance")
    assert not is_within("financed", "finance")
    assert parent("a/b/c") == "a/b"
    assert parent("a") is None


def test_parse_query_takes_out_known_filters_only() -> None:
    parsed = parse_query(
        'rent tag:"home/lease" kind:Document amount>=100 doc_type:invoice http://x',
        {"amount", "doc_type"},
    )
    assert parsed.words == "rent http://x"
    assert parsed.tags == ("home/lease",)
    assert parsed.kind == "document"
    assert parsed.properties == (
        PropertyFilter("amount", ">=", "100"),
        PropertyFilter("doc_type", ":", "invoice"),
    )
    assert parse_query("vendor:acme", set()).words == "vendor:acme"


def test_fields_schema_and_cleaning() -> None:
    names = extract.field_names(("vendor", "date", "Not Valid", "vendor"))
    assert names == (*extract.DEFAULT_FIELDS, "vendor")
    model = extract.schema(names)
    answer = model.model_validate(
        {
            "doc_type": "Brochure",
            "title": "  A   title ",
            "date": "2024-02-30",
            "sender": "N/A",
            "amount": 1e20,
            "currency": "usd",
            "vendor": "Acme",
        }
    )
    assert extract.clean(answer, 200) == {
        "doc_type": "other",
        "title": "A title",
        "date": None,
        "sender": None,
        "recipient": None,
        "amount": None,
        "currency": "USD",
        "vendor": "Acme",
    }
    partial = model.model_validate({"date": "2024-7", "amount": 3})
    assert extract.clean(partial, 200)["date"] == "2024-07"
    assert extract.clean(partial, 200)["amount"] == 3.0
    with pytest.raises(ValidationError):
        model.model_validate({"amount": "lots"})


def test_tag_schema_only_allows_the_vocabulary() -> None:
    model = apply.schema(("finance/receipt", "home"), 2)
    assert model.model_validate({"tags": ["home"]}).tags == ["home"]  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        model.model_validate({"tags": ["invented"]})
    with pytest.raises(ValidationError):
        model.model_validate({"tags": ["home", "home", "home"]})


def test_tree_adds_parents_that_are_not_tags() -> None:
    tags = [TagInfo("finance/receipt", "active"), TagInfo("home", "active")]
    rows = tree(tags)
    assert [(r.name, r.depth, r.tag is not None) for r in rows] == [
        ("finance", 0, False),
        ("finance/receipt", 1, True),
        ("home", 0, True),
    ]


def test_tag_file_parsing() -> None:
    parsed = parse("tags:\n  - name: a\n    aliases: [b]\n  - name: c\n    description: d\n")
    assert [e.name for e in parsed.tags] == ["a", "c"]
    assert parse("- name: x\n").tags[0].name == "x"
    with pytest.raises(TagError):
        parse("tags: {")
    with pytest.raises(TagError):
        parse("just text")
