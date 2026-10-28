# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
"""Effective folder settings, computed in memory."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from steward.core.folders import (
    FolderMapping,
    FolderRef,
    Library,
    PathError,
    Policy,
    Role,
    TypeDefinition,
    resolve_types,
)

TYPES = resolve_types({}, {"receipts": TypeDefinition(extends="documents", fields=("vendor",))})


def policy(
    mappings: dict[str, tuple[str | None, bool | None, bool]],
    roles: dict[Role, str] | None = None,
    library: str = "lib",
) -> Policy:
    """mappings: relpath -> (type, locked override, permanent)."""
    built = {}
    for i, (relpath, (type_name, locked, permanent)) in enumerate(mappings.items()):
        ref = FolderRef(library, relpath)
        built[ref] = FolderMapping(i, ref, type_name, locked, permanent)
    return Policy(
        {library: Library(library, library, "/x", 0)},
        TYPES,
        built,
        {role: FolderRef(library, path) for role, path in (roles or {}).items()},
    )


def at(p: Policy, relpath: str, library: str = "lib") -> object:
    return p.settings_for(FolderRef(library, relpath))


def test_unmapped_library_is_general() -> None:
    s = policy({}).settings_for(FolderRef("lib", "any/where"))
    assert (s.type.name, s.type_from, s.sensitivity, s.locked) == ("general", None, "normal", False)


def test_unknown_library_fails() -> None:
    with pytest.raises(PathError, match="unknown library"):
        policy({}).settings_for(FolderRef("other", "x"))


def test_most_specific_mapping_wins_and_inherits_down() -> None:
    p = policy({"Docs": ("documents", None, False), "Docs/Receipts": ("receipts", None, False)})
    deep = p.settings_for(FolderRef("lib", "Docs/Receipts/2026/March"))
    assert (deep.type.name, deep.type_from, deep.fields) == (
        "receipts",
        "Docs/Receipts",
        ("vendor",),
    )
    assert p.settings_for(FolderRef("lib", "Docs/Other")).type.name == "documents"
    assert p.settings_for(FolderRef("lib", "Elsewhere")).type.name == "general"


def test_restrictions_carry_down_even_into_less_strict_types() -> None:
    p = policy(
        {
            "Private": ("private", None, False),
            "Private/Shared": ("general", None, False),
            "App": ("app-managed", None, False),
            "App/Sub": ("documents", None, False),
            "Hidden": ("ignored", None, False),
            "Hidden/Sub": ("general", None, False),
        }
    )
    inside_private = p.settings_for(FolderRef("lib", "Private/Shared/x"))
    assert inside_private.type.name == "general"
    assert inside_private.sensitivity == "local-only"
    assert inside_private.locked
    assert inside_private.locked_by == "Private"
    assert not inside_private.shareable
    assert not inside_private.triage_target
    assert inside_private.notes

    inside_app = p.settings_for(FolderRef("lib", "App/Sub"))
    assert not inside_app.writable
    assert not inside_app.triage_target

    assert not p.settings_for(FolderRef("lib", "Hidden/Sub")).visible


def test_lock_override_on_a_folder_and_its_contents() -> None:
    p = policy({"Taxes": (None, True, False), "Taxes/Public": (None, False, False)})
    assert p.settings_for(FolderRef("lib", "Taxes/2026")).locked
    # A child can't unlock what a parent locked.
    child = p.settings_for(FolderRef("lib", "Taxes/Public"))
    assert child.locked
    assert child.locked_by == "Taxes"


def test_unlocking_a_locked_type_folder() -> None:
    p = policy({"Private": ("private", False, False)})
    s = p.settings_for(FolderRef("lib", "Private/x"))
    assert not s.locked
    assert s.sensitivity == "local-only"  # unlocking doesn't lower sensitivity


def test_permanent_lock_survives_a_type_change() -> None:
    p = policy({"Private": ("general", True, True)})
    s = p.settings_for(FolderRef("lib", "Private/a"))
    assert (s.locked, s.permanent, s.locked_by) == (True, True, "Private")


def test_copy_holding_roles_are_protected() -> None:
    p = policy(
        {"Backups": ("general", None, False), "Trash": ("general", None, False)},
        roles={"edit_backups": "Backups", "trash": "Trash", "inbox": "Inbox"},
    )
    for path in ("Backups/2026/01/7/file", "Trash"):
        s = p.settings_for(FolderRef("lib", path))
        assert (s.sensitivity, s.shareable, s.triage_target) == ("local-only", False, False)
    inbox = p.settings_for(FolderRef("lib", "Inbox"))
    assert (inbox.sensitivity, inbox.shareable) == ("normal", True)
    assert p.role_at(FolderRef("lib", "Trash/x")) == ["trash"]


@dataclass(frozen=True)
class Layout:
    """The same intent expressed with two different folder layouts and mappings."""

    name: str
    library: str
    mappings: dict[str, tuple[str | None, bool | None, bool]]
    private: str
    receipts: str
    photos: str
    other_app: str
    inbox: str


LAYOUTS = [
    Layout(
        "numbered",
        "data",
        {
            "00-inbox": ("inbox", None, False),
            "10-media": ("media", None, False),
            "10-media/app": ("app-managed", None, False),
            "20-docs": ("documents", None, False),
            "20-docs/receipts": ("receipts", None, False),
            "90-private": ("private", True, True),
        },
        private="90-private/taxes",
        receipts="20-docs/receipts/2026",
        photos="10-media/2026/trip",
        other_app="10-media/app/library",
        inbox="00-inbox",
    ),
    Layout(
        "flat",
        "home",
        {
            "Inbox": ("inbox", None, False),
            "Photos": ("media", None, False),
            "Photos/Managed by another app": ("app-managed", None, False),
            "Documents": ("documents", None, False),
            "Documents/Receipts": ("receipts", None, False),
            "Private": ("private", None, False),
        },
        private="Private/Taxes",
        receipts="Documents/Receipts/2026",
        photos="Photos/Trip",
        other_app="Photos/Managed by another app/x",
        inbox="Inbox",
    ),
]


@pytest.mark.parametrize("layout", LAYOUTS, ids=lambda layout: layout.name)
def test_equivalent_behavior_on_different_layouts(layout: Layout) -> None:
    p = policy(layout.mappings, library=layout.library)

    def s(relpath: str) -> object:
        return p.settings_for(FolderRef(layout.library, relpath))

    private = s(layout.private)
    assert (private.locked, private.sensitivity, private.shareable) == (True, "local-only", False)  # type: ignore[attr-defined]
    assert s(layout.receipts).fields == ("vendor",)  # type: ignore[attr-defined]
    assert "caption" in s(layout.photos).stages_add  # type: ignore[attr-defined]
    assert not s(layout.other_app).writable  # type: ignore[attr-defined]
    assert s(layout.inbox).type.name == "inbox"  # type: ignore[attr-defined]
    assert not s(layout.inbox).triage_target  # type: ignore[attr-defined]
