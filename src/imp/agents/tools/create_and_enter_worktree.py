from __future__ import annotations

from typing import Any

from imp.agents.tools import imp_tool


@imp_tool(
    name="create_and_enter_worktree",
    description=(
        "Go live on this draft: fork the branch and worktree from the base "
        "branch's current tip and switch this Quest into it. Your subsequent "
        "Read/Bash/edit tools operate in the new worktree immediately. Reports "
        "whether the base branch advanced since the draft was created "
        "(head_changed) so you can re-ground the spec against current code. "
        "Idempotent — a no-op once the worktree already exists."
    ),
)
def create_and_enter_worktree(ctx: Any) -> str:
    if ctx.change_state is None:
        raise RuntimeError(
            "create_and_enter_worktree is only available to top-level Quest steps")
    change = ctx.change
    head_changed = change.materialize()
    ctx.project_root = change.project_root
    return (f"materialized worktree at {change.project_root}; "
            f"head_changed={'true' if head_changed else 'false'}")
