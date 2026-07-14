from __future__ import annotations

from typing import Any

from imp.agents.tools import imp_tool
from imp.engine import backlog
from imp.engine.quest import Quest, live_stems
from imp.engine.quest_id import new_quest_id
from imp.engine.state import JsonlEventLog


@imp_tool(
    name="create_quest",
    description=(
        "Record a durable follow-up backlog quest (no branch/worktree yet). "
        "Give it a short title and a self-contained description that needs no "
        "other context; optionally list quest ids it depends_on. Pass "
        "workflow= to run it on a named workflow instead of the project "
        "default, and start=True to launch it immediately as a detached, "
        "autonomous background run (it appears in the active list); otherwise "
        "it is recorded in the backlog and startable later with "
        "`imp resume <id>`."
    ),
)
def create_quest(
    ctx: Any, title: str, description: str,
    depends_on: list[str] | None = None,
    workflow: str | None = None, start: bool = False,
) -> str:
    if ctx.quest_state is None:
        raise RuntimeError("create_quest is only available to top-level quest steps")
    parent = ctx.quest
    new_id = new_quest_id(existing_stems=live_stems(parent.control_root))
    deps = list(depends_on or [])
    seed = backlog.seed_opening({"title": title, "description": description})
    Quest.create_draft(
        parent.control_root, title=title, quest_id=new_id,
        base_branch=parent.base_branch, depends_on=deps, workflow=workflow,
        origin="draft" if start else "backlog",
        seed_opening=seed, defer_seed=True)
    kind = "started" if start else "backlog"
    log = ctx.event_log or JsonlEventLog(ctx.events_path)
    log.append(
        agent_id=ctx.agent_id, type="quest_created",
        payload={"id": new_id, "title": title, "kind": kind,
                 "depends_on": deps})
    if start:
        from imp.remote.daemon import spawn
        spawn.start_quest(parent.control_root, new_id)
        return (f"started quest {new_id}; it is running autonomously in the "
                f"background and appears in the active list")
    return f"recorded backlog quest {new_id}; start it later with `imp resume {new_id}`"
