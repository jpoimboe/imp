from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from imp.agents.tools import imp_tool
from imp.engine import backlog
from imp.engine.slug import new_change_id
from imp.engine.state import JsonlEventLog


@imp_tool(
    name="create_change",
    description=(
        "Record a durable follow-up backlog Change (no branch/worktree yet). "
        "Give it a short title and a self-contained brief that needs no other "
        "context; optionally list change ids it depends_on. Returns the new id; "
        "it is startable later with `imp resume <id>`."
    ),
)
def create_change(
    ctx: Any, title: str, brief: str, depends_on: list[str] | None = None,
) -> str:
    if ctx.change_state is None:
        raise RuntimeError("create_change is only available to top-level Change steps")
    control_root = ctx.change.control_root
    new_id = new_change_id(date=datetime.now(UTC).strftime("%Y-%m-%d"))
    deps = list(depends_on or [])
    backlog.write_record(
        control_root, change_id=new_id, title=title, brief=brief,
        depends_on=deps, created_by=ctx.agent_id or "agent")
    log = ctx.event_log or JsonlEventLog(ctx.events_path)
    log.append(
        agent_id=ctx.agent_id, type="change_created",
        payload={"id": new_id, "title": title, "kind": "backlog",
                 "depends_on": deps})
    return f"recorded backlog Change {new_id}; start it later with `imp resume {new_id}`"
