# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""rename_quest tool (spec §3, §10). Applies the rename IMMEDIATELY inside the
tool call and returns the quest id so the agent can write the spec at the
quest-home's artifacts/spec/<id>.md. Content-only: rewrites the gitignored
quest.json, touches no git. Top-level only — raises for subagents
(no quest_state)."""

from __future__ import annotations

from typing import Any

from imp.agents.tools import imp_tool
from imp.engine.state import JsonlEventLog


@imp_tool(
    name="rename_quest",
    description=(
        "Rename this quest to a meaningful title once you understand the work. "
        "A title is a short human-readable phrase — a few words with spaces, "
        "sentence case, like a PR title — not a branch-name slug (no kebab-case "
        "hyphens). Applies immediately and returns this quest's id; use that id "
        "when you write the spec at the quest-home's artifacts/spec/<id>.md."
    ),
)
def rename_quest(ctx: Any, name: str) -> str:
    title = name.strip()
    if not title:
        raise ValueError("rename_quest requires a non-empty name")
    if ctx.quest_state is None:
        raise RuntimeError("rename_quest is only available to top-level quest steps")
    quest = ctx.quest
    if title == quest.title:
        return f"already named {quest.id}"
    log = ctx.event_log or JsonlEventLog(ctx.events_path)
    quest.rename(title, by="agent", log=log)
    return (f"renamed to {title!r}; use "
            f"{quest.quest_dir}/artifacts/spec/{quest.id}.md for the spec")
