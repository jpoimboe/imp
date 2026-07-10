from __future__ import annotations

from typing import Any

from imp.agents.tools import imp_tool
from imp.engine import backlog
from imp.engine.change import Change, live_stems
from imp.engine.change_id import new_change_id
from imp.engine.state import JsonlEventLog


@imp_tool(
    name="create_change",
    description=(
        "Record a durable follow-up backlog Change (no branch/worktree yet). "
        "Give it a short title and a self-contained description that needs no "
        "other context; optionally list change ids it depends_on. Returns the "
        "new id; it is startable later with `imp resume <id>`."
    ),
)
def create_change(
    ctx: Any, title: str, description: str, depends_on: list[str] | None = None,
) -> str:
    if ctx.change_state is None:
        raise RuntimeError("create_change is only available to top-level Change steps")
    parent = ctx.change
    new_id = new_change_id(existing_stems=live_stems(parent.control_root))
    deps = list(depends_on or [])
    seed = backlog.seed_opening({"title": title, "description": description})
    Change.create_draft(
        parent.control_root, title=title, change_id=new_id,
        base_branch=parent.base_branch, depends_on=deps,
        origin="followup", seed_opening=seed)
    log = ctx.event_log or JsonlEventLog(ctx.events_path)
    log.append(
        agent_id=ctx.agent_id, type="change_created",
        payload={"id": new_id, "title": title, "kind": "backlog",
                 "depends_on": deps})
    return f"recorded backlog Change {new_id}; start it later with `imp resume {new_id}`"
