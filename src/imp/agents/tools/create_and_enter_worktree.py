# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
from __future__ import annotations

import dataclasses
import functools
import json
import subprocess
from typing import Any, cast

import anyio.to_thread

from imp.agents.tools import imp_tool
from imp.agents.tools.errors import FatalToolError
from imp.engine import backlog
from imp.engine.env import engine_env
from imp.engine.feedback import truncate_detail
from imp.engine.gates import ScriptOutputExceeded, run_command
from imp.engine.sandbox import SandboxOpenFailed
from imp.engine.shim import imp_shim_dir

WORKTREE_SETUP_TIMEOUT_S = 600


def _declared_deps(quest: Any) -> list[str]:
    meta = json.loads((quest.quest_dir / "quest.json").read_text())
    return list(meta.get("depends_on", []))


def _check_dependencies(ctx: Any, quest: Any) -> bool:
    """True when a dep is unmerged: flag the ids and stop the caller."""
    deps = _declared_deps(quest)
    if not deps:
        return False
    _, unmerged = backlog.readiness(quest.control_root, deps)
    if not unmerged:
        return False
    ctx.dependency_wait = tuple(unmerged)
    return True


def _setup_output_body(stdout: str | None, stderr: str | None) -> str:
    raw = f"{stdout or ''}\n{stderr or ''}"
    body = truncate_detail(raw)
    return f"\n\n{body}" if body.strip() else ""


async def _run_worktree_setup(ctx: Any, quest: Any) -> None:
    """Run worktree_setup in the fresh worktree; config resolves from the main checkout."""
    from imp.providers.effective import load_effective_config

    command = load_effective_config(
        control_root=quest.control_root).config.worktree_setup
    if not command:
        return
    call = functools.partial(
        run_command, command=command, cwd=str(quest.project_root),
        timeout_seconds=WORKTREE_SETUP_TIMEOUT_S,
        env_extra=engine_env(
            project_root=quest.project_root, quest_id=quest.id,
            quest_title=quest.title, quest_dir=quest.quest_dir,
            control_root=quest.control_root, test_gates=ctx.test_gates,
            pythonpath=ctx.pythonpath),
        policy=ctx.sandbox_policy, shim_dir=str(imp_shim_dir(quest.quest_dir)))
    try:
        out = await anyio.to_thread.run_sync(call)
    except subprocess.TimeoutExpired as exc:
        # run_command attaches both streams already decoded; typeshed types them as bytes
        raise FatalToolError(
            f"worktree_setup timed out after {WORKTREE_SETUP_TIMEOUT_S}s: "
            f"{command}"
            + _setup_output_body(
                cast(str | None, exc.output), cast(str | None, exc.stderr)
            )) from exc
    except ScriptOutputExceeded as exc:
        raise FatalToolError(
            f"worktree_setup output cap exceeded: {command}: {exc}") from exc
    except SandboxOpenFailed as exc:
        raise FatalToolError(f"worktree_setup failed: {exc}") from exc
    if out["exit"] != 0:
        raise FatalToolError(
            f"worktree_setup failed (exit {out['exit']}): {command}"
            + _setup_output_body(out["stdout"], out["stderr"]))


@imp_tool(
    name="create_and_enter_worktree",
    description=(
        "Go live on this draft: fork the branch and worktree from the base "
        "branch's current tip and switch this quest into it. Your subsequent "
        "Read/Bash/edit tools operate in the new worktree immediately. Reports "
        "whether the base branch advanced since the draft was created "
        "(head_changed) so you can re-ground the spec against current code. If "
        "this quest depends on a quest that hasn't merged yet, it first waits "
        "for that merge before forking — an attended wait is interruptible "
        "(Ctrl-D pauses; a later resume retries), and a background run waits "
        "on its own until the dependency lands. Idempotent — once the worktree "
        "exists, creating it again is a no-op, but the project's "
        "`worktree_setup` command re-runs on every call."
    ),
)
async def create_and_enter_worktree(ctx: Any) -> str:
    if ctx.quest_state is None:
        raise RuntimeError(
            "create_and_enter_worktree is only available to top-level quest steps")
    quest = ctx.quest
    if not quest.materialized and _check_dependencies(ctx, quest):
        ids = ", ".join(ctx.dependency_wait)
        return (f"waiting for {ids} to merge — the run will re-enter this step "
                "automatically once the dependency lands; take no further action.")
    head_changed = quest.materialize()
    ctx.project_root = quest.project_root
    if ctx.base_sandbox_policy is not None:
        ctx.base_sandbox_policy = dataclasses.replace(
            ctx.base_sandbox_policy,
            worktree=str(quest.project_root),
            git_worktree_self=str(
                quest.control_root / ".git" / "worktrees" / quest.project_root.name),
        )
    await _run_worktree_setup(ctx, quest)
    return (f"materialized worktree at {quest.project_root}; "
            f"head_changed={'true' if head_changed else 'false'}")
