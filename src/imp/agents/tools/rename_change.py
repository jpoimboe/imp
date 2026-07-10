"""rename_change tool (spec §3, §10). Applies the rename IMMEDIATELY inside the
tool call and returns the Change id so the agent writes the spec at the
change-home's artifacts/spec/<id>.md in-band, same turn. Content-only: rewrites the
gitignored change.json, touches no git. Top-level only — raises for subagents
(no change_state)."""

from __future__ import annotations

from typing import Any

from imp.agents.tools import imp_tool
from imp.engine.state import JsonlEventLog


@imp_tool(
    name="rename_change",
    description=(
        "Rename this Change to a meaningful title once you understand the work. "
        "A title is a short human-readable phrase — a few words with spaces, "
        "sentence case, like a PR title — not a branch-name slug (no kebab-case "
        "hyphens). Applies immediately and returns this Change's id; use that id "
        "to write and commit the spec at the change-home's artifacts/spec/<id>.md "
        "this same turn."
    ),
)
def rename_change(ctx: Any, name: str) -> str:
    title = name.strip()
    if not title:
        raise ValueError("rename_change requires a non-empty name")
    if ctx.change_state is None:
        raise RuntimeError("rename_change is only available to top-level Change steps")
    change = ctx.change
    if title == change.title:
        return f"already named {change.id}"
    log = ctx.event_log or JsonlEventLog(ctx.events_path)
    change.rename(title, log=log)
    return (f"renamed to {title!r}; use "
            f"{change.quest_dir}/artifacts/spec/{change.id}.md for the spec")
