from __future__ import annotations

from typing import Any

from imp.agents.tools import imp_tool


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
def create_and_enter_worktree(ctx: Any) -> str:
    if ctx.quest_state is None:
        raise RuntimeError(
            "create_and_enter_worktree is only available to top-level quest steps")
    quest = ctx.quest
    head_changed = quest.materialize()
    ctx.project_root = quest.project_root
    return (f"materialized worktree at {quest.project_root}; "
            f"head_changed={'true' if head_changed else 'false'}")
