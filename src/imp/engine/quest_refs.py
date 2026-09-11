# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Resolve a quest's parent/dependency ids into display shapes for prompts and imp show."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from imp.engine.archive import read as archive_read
from imp.engine.errors import ArchiveMismatch
from imp.paths import project_state_dir

if TYPE_CHECKING:
    from imp.engine.quest import Quest


def _title(control_root: Path, quest_id: str) -> str:
    """The quest's current title: live home, then archive, then the id itself as a fallback."""
    home = project_state_dir(control_root) / "quests" / quest_id / "quest.json"
    if home.exists():
        try:
            return str(json.loads(home.read_bytes())["title"])
        except (json.JSONDecodeError, KeyError):
            return quest_id
    try:
        raw = archive_read.read_quest_json(control_root, quest_id)
    except ArchiveMismatch:
        return quest_id
    if raw is not None:
        try:
            return str(json.loads(raw)["title"])
        except (json.JSONDecodeError, KeyError):
            return quest_id
    return quest_id


def resolve_parent(
    control_root: Path, parent_id: str | None,
) -> dict[str, str] | None:
    if not parent_id:
        return None
    return {"id": parent_id, "title": _title(control_root, parent_id)}


def resolve_dependencies(
    control_root: Path, depends_on: list[str],
) -> list[dict[str, Any]]:
    return [
        {"id": d, "title": _title(control_root, d),
         "merged": archive_read.is_archived(control_root, d)}
        for d in depends_on
    ]


def resolve_for_quest(
    quest: Quest,
) -> tuple[dict[str, str] | None, list[dict[str, Any]]]:
    """Resolve both refs off a loaded quest into the prompt-context pairing."""
    return (
        resolve_parent(quest.control_root, quest.parent),
        resolve_dependencies(quest.control_root, quest.depends_on),
    )
