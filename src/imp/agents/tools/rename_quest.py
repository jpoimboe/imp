"""rename_quest tool (spec §3, §10). Applies the rename IMMEDIATELY inside the
tool call and returns the quest id so the agent writes the spec at the
change-home's artifacts/spec/<id>.md in-band, same turn. Content-only: rewrites the
gitignored quest.json, touches no git. Top-level only — raises for subagents
(no change_state)."""

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
        "to write and commit the spec at the quest-home's artifacts/spec/<id>.md "
        "this same turn."
    ),
)
def rename_quest(ctx: Any, name: str) -> str:
    title = name.strip()
    if not title:
        raise ValueError("rename_quest requires a non-empty name")
    if ctx.change_state is None:
        raise RuntimeError("rename_quest is only available to top-level quest steps")
    quest = ctx.quest
    if title == quest.title:
        return f"already named {quest.id}"
    log = ctx.event_log or JsonlEventLog(ctx.events_path)
    quest.rename(title, log=log)
    return (f"renamed to {title!r}; use "
            f"{quest.quest_dir}/artifacts/spec/{quest.id}.md for the spec")
