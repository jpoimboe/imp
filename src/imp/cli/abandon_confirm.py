# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Pinned copy for confirming the teardown of a built quest."""
from __future__ import annotations

from typing import TYPE_CHECKING

from imp.engine import worktree

if TYPE_CHECKING:
    from imp.engine.quest import Quest


def removal_description(quest: Quest) -> str | None:
    """What abandoning `quest` removes; None for a draft, which has nothing built."""
    if not quest.materialized:
        return None
    n = worktree.commit_count(quest.control_root, quest.id, quest.base_branch)
    count = "no commits" if n == 0 else "1 commit" if n == 1 else f"{n} commits"
    return f"worktree and branch imp/{quest.id} ({count})"


def abandon_question(quest_id: str, removed: str) -> str:
    return f"Abandon {quest_id}? This removes its {removed}. [y/N] "
