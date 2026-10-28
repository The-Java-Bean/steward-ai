# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

import pytest

from steward.core.folders import (
    BUILTIN_TYPES,
    FolderRef,
    FolderTypeError,
    PathError,
    StageChanges,
    TypeDefinition,
    TypeSettings,
    ancestors,
    is_within,
    normalize_relpath,
    resolve_types,
)
from steward.core.folders.types import max_sensitivity

# --- paths -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("/", ""),
        ("a/b/", "a/b"),
        ("/a/b", "a/b"),
        ("a\\b", "a/b"),
        ("Café", "Café"),  # NFD -> NFC
        ("with space/x", "with space/x"),
    ],
)
def test_normalize_relpath(raw: str, expected: str) -> None:
    assert normalize_relpath(raw) == expected


@pytest.mark.parametrize("raw", ["a//b", "a/../b", "./a", "a/.", "a\x00b", "x" * 256])
def test_normalize_relpath_rejects(raw: str) -> None:
    with pytest.raises(PathError):
        normalize_relpath(raw)


def test_ancestors_and_is_within() -> None:
    assert ancestors("") == [""]
    assert ancestors("a/b/c") == ["", "a", "a/b", "a/b/c"]
    assert is_within("a/b", "a")
    assert is_within("a", "a")
    assert is_within("anything", "")
    assert not is_within("ab", "a")


def test_folder_ref_parsing() -> None:
    assert FolderRef.parse("main:Documents/x/") == FolderRef("main", "Documents/x")
    assert str(FolderRef.parse("main:")) == "main:"
    for bad in ("main", "Main:x", "1lib:x", "main:../x"):
        with pytest.raises(PathError):
            FolderRef.parse(bad)


# --- types -----------------------------------------------------------------------


def test_builtins_resolve_with_every_setting() -> None:
    types = resolve_types({}, {})
    assert set(types) == set(BUILTIN_TYPES)
    assert types["private"].sensitivity == "local-only"
    assert types["private"].locked
    assert not types["private"].shareable
    assert types["ignored"].index == "none"
    assert not types["ignored"].visible
    assert types["archive"].hash == "sampled"
    assert types["inbox"].chain == ("inbox", "general")
    assert types["general"].chain == ("general",)


def test_custom_types_inherit_along_extends() -> None:
    types = resolve_types(
        {},
        {
            "receipts": TypeDefinition(extends="documents", fields=("vendor",)),
            "old_receipts": TypeDefinition(extends="receipts", index="metadata"),
            "plain": TypeDefinition(description="no extends means general"),
        },
    )
    old = types["old_receipts"]
    assert old.chain == ("old_receipts", "receipts", "documents", "general")
    assert (old.index, old.fields, old.description) == (
        "metadata",
        ("vendor",),
        BUILTIN_TYPES["documents"].description,
    )
    assert not old.builtin
    assert types["plain"].chain == ("plain", "general")


def test_overriding_a_builtin_affects_types_that_extend_it() -> None:
    types = resolve_types(
        {"documents": TypeSettings(hash="sampled")},
        {"receipts": TypeDefinition(extends="documents")},
    )
    assert types["documents"].hash == "sampled"
    assert types["receipts"].hash == "sampled"
    assert types["documents"].description == BUILTIN_TYPES["documents"].description


def test_stage_changes_accumulate() -> None:
    types = resolve_types(
        {},
        {
            "scans": TypeDefinition(extends="documents", stages=StageChanges(add=("ocr",))),
            "photo_scans": TypeDefinition(
                extends="scans", stages=StageChanges(add=("caption",), remove=("ocr",))
            ),
            "media_plus": TypeDefinition(extends="media", stages=StageChanges(add=("extract",))),
        },
    )
    assert types["scans"].stages_add == {"ocr"}
    assert types["photo_scans"].stages_add == {"caption"}
    assert types["photo_scans"].stages_remove == {"ocr"}
    assert types["media_plus"].stages_add == {"exif", "caption", "extract"}
    assert types["media_plus"].stages_remove == {"ocr", "chunk"}


@pytest.mark.parametrize(
    ("overrides", "custom", "message"),
    [
        ({}, {"a": TypeDefinition(extends="nope")}, "unknown type 'nope'"),
        ({}, {"a": TypeDefinition(extends="b"), "b": TypeDefinition(extends="a")}, "cycle"),
        ({}, {"documents": TypeDefinition()}, "built-in names"),
        ({"receipts": TypeSettings()}, {}, "not built-in"),
        ({}, {"Bad Name": TypeDefinition()}, "must be lowercase"),
    ],
)
def test_invalid_type_definitions(
    overrides: dict[str, TypeSettings], custom: dict[str, TypeDefinition], message: str
) -> None:
    with pytest.raises(FolderTypeError, match=message):
        resolve_types(overrides, custom)


def test_unknown_settings_are_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TypeDefinition.model_validate({"indx": "full"})
    with pytest.raises(ValidationError):
        TypeDefinition.model_validate({"sensitivity": "secret"})


def test_sensitivity_order() -> None:
    assert max_sensitivity("normal", "local-only") == "local-only"
    assert max_sensitivity("excluded", "normal", "local-only") == "excluded"
