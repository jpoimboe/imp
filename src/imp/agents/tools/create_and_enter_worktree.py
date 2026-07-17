from __future__ import annotations

import json
from typing import Any

from imp.agents.tools import imp_tool
from imp.engine import backlog
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.engine.backoff import interruptible_sleep

_DEP_POLL_INTERVAL = 2.0


def _declared_deps(quest: Any) -> list[str]:
    meta = json.loads((quest.quest_dir / "quest.json").read_text())
    return list(meta.get("depends_on", []))


def _emit(ctx: Any, type_: str, payload: dict[str, Any]) -> None:
    if ctx.event_log is not None:
        ctx.event_log.append(
            agent_id=ctx.agent_id, type=type_, payload=payload)


async def _await_dependencies(ctx: Any, quest: Any) -> None:
    deps = _declared_deps(quest)
    if not deps:
        return
    _, unmerged = backlog.readiness(quest.control_root, deps)
    if not unmerged:
        return
    if ctx.non_interactive:
        # No one is attached to watch the wait: park cleanly, resumable later.
        raise AwaitingUserInput()
    _emit(ctx, "dependency_waiting", {"depends_on": unmerged})
    coord = ctx.coordinator
    while True:
        if coord is not None and getattr(coord, "should_pause", False):
            # Ctrl-D: pause the step so a later `imp resume` retries the wait.
            raise AwaitingUserInput(interrupt=True)
        _, unmerged = backlog.readiness(quest.control_root, deps)
        if not unmerged:
            break
        await interruptible_sleep(_DEP_POLL_INTERVAL, coord)
    _emit(ctx, "dependency_ready", {})


@imp_tool(
    name="create_and_enter_worktree",
    description=(
        "Go live on this draft: fork the branch and worktree from the base "
        "branch's current tip and switch this quest into it. Your subsequent "
        "Read/Bash/edit tools operate in the new worktree immediately. Reports "
        "whether the base branch advanced since the draft was created "
        "(head_changed) so you can re-ground the spec against current code. If "
        "this quest depends on a quest that hasn't merged yet, it first waits "
        "for that merge before forking — the wait is interruptible (Ctrl-D "
        "pauses; a later resume retries). Idempotent — a no-op once the "
        "worktree already exists."
    ),
)
async def create_and_enter_worktree(ctx: Any) -> str:
    if ctx.quest_state is None:
        raise RuntimeError(
            "create_and_enter_worktree is only available to top-level quest steps")
    quest = ctx.quest
    if not quest.materialized:
        await _await_dependencies(ctx, quest)
    head_changed = quest.materialize()
    ctx.project_root = quest.project_root
    return (f"materialized worktree at {quest.project_root}; "
            f"head_changed={'true' if head_changed else 'false'}")
