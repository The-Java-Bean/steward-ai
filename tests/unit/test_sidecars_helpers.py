# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""What a sidecar should say and how it merges with what's there (no exiftool)."""

from __future__ import annotations

from steward.modules.photos.sidecars import (
    EMPTY_XMP,
    Current,
    Edit,
    Record,
    Wanted,
    edit_args,
    looks_like_xmp,
    merge,
    wanted,
)
from steward.sdk import FileTag


def test_a_tag_gives_every_level_and_its_path() -> None:
    want = wanted(
        [FileTag("finance/receipt", "user"), FileTag("trips", "ai")], "  A dog\n on a beach. "
    )
    assert want.subject == ("finance", "receipt", "trips")
    assert want.hierarchy == ("finance|receipt", "trips")
    assert want.description == "A dog on a beach."


def test_a_pipe_in_a_tag_name_cant_add_a_level() -> None:
    assert wanted([FileTag("a|b/c", "user")], None).hierarchy == ("a b|c",)


def test_nothing_to_say_is_empty() -> None:
    assert wanted([], "   ").empty


def test_what_counts_as_xmp() -> None:
    assert looks_like_xmp(EMPTY_XMP)
    assert looks_like_xmp(b"\n<x:xmpmeta xmlns:x='adobe:ns:meta/'></x:xmpmeta>")
    assert looks_like_xmp(b"<?xml version='1.0'?><rdf:RDF></rdf:RDF>")
    assert not looks_like_xmp(b"AT&TFORM\x00\x00 a DjVu file")
    assert not looks_like_xmp(b"\xff\xd8\xff a JPEG named .xmp")
    assert not looks_like_xmp(b"<?xml version='1.0'?><!DOCTYPE x [<!ENTITY a 'b'>]><x/>")
    assert not looks_like_xmp(b"<?xml version='1.0'?><!entity a 'b'>")


def test_a_new_sidecar_gets_everything_and_owns_it() -> None:
    edit, record = merge(Wanted(("a",), ("a",), "Hello"), None, Current())
    assert edit == Edit(("a",), (), ("a",), (), True, "Hello")
    assert record.owned_subject == ("a",)
    assert record.owned_description


def test_a_keyword_already_there_isnt_owned() -> None:
    edit, record = merge(Wanted(("a",)), None, Current(frozenset({"a"})))
    assert record.owned_subject == ()
    # ...so it stays when steward-ai stops wanting it
    edit, _ = merge(Wanted(), record, Current(frozenset({"a"})))
    assert edit.empty


def test_only_owned_keywords_are_removed() -> None:
    record = Record(subject=("mine", "theirs"), owned_subject=("mine",))
    edit, after = merge(Wanted(), record, Current(frozenset({"mine", "theirs", "other"})))
    assert edit.remove_subject == ("mine",)
    assert after.subject == ()


def test_a_keyword_another_program_removed_stays_removed() -> None:
    record = Record(subject=("mine",), owned_subject=("mine",))
    edit, after = merge(Wanted(("mine", "new")), record, Current())
    assert edit.add_subject == ("new",)
    assert after.subject == ("mine", "new")
    assert after.owned_subject == ("new",)


def test_a_description_is_only_written_over_nothing_or_our_own() -> None:
    _, ours = merge(Wanted(description="Old"), None, Current())
    edit, after = merge(Wanted(description="New"), ours, Current(description="Old"))
    assert (edit.set_description, edit.description) == (True, "New")
    assert after.owned_description
    edit, after = merge(Wanted(description="New"), ours, Current(description="Someone else's"))
    assert not edit.set_description
    assert not after.owned_description
    assert after.matches(Wanted(description="New"))


def test_our_description_goes_when_there_is_none_to_want() -> None:
    _, ours = merge(Wanted(description="Old"), None, Current())
    edit, after = merge(Wanted(), ours, Current(description="Old"))
    assert (edit.set_description, edit.description) == (True, None)
    assert not after.owned_description


def test_records_survive_json() -> None:
    _, record = merge(Wanted(("a",), ("a",), "Hello"), None, Current())
    assert Record.from_dict(record.as_dict()) == record


def test_arguments() -> None:
    edit = Edit(("new",), ("old",), ("x|y",), (), True, "A b")
    assert edit_args(edit) == [
        "-XMP-dc:Subject-=old",
        "-XMP-dc:Subject-=new",
        "-XMP-dc:Subject+=new",
        "-XMP-lr:HierarchicalSubject-=x|y",
        "-XMP-lr:HierarchicalSubject+=x|y",
        "-XMP-dc:Description=A b",
    ]
