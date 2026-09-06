"""Test doubles for a step that goes live mid-turn and is then re-entered.

`GoLiveOnce` wraps any AgentRunner and performs the production go-live call
before the first drive streams, reproducing the window in which the engine's
already-baked sandbox policy is still rooted at the control root.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner
from imp.engine.sandbox import SandboxPolicy


def inactive_policy_for(root: Path) -> SandboxPolicy:
    """An inactive policy rooted like a real one, so the reconcile's field
    assertions hold on a host with or without bwrap."""
    return SandboxPolicy(
        active=False, worktree=str(root), git_common_dir=str(root / ".git"),
        git_ro_remask=(), writable_caches=(), extra_readable=(),
        extra_writable=(), secret_dirs=(), secret_files=(), setenv=(),
        network=True, note="test")


def patch_policy_builder(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Swap the executor's policy build for a deterministic inactive policy
    rooted at quest.project_root. Returns the build counter."""
    builds: dict[str, int] = {"n": 0}

    def _build(quest: Any, _config: Any, _log: Any) -> SandboxPolicy:
        builds["n"] += 1
        return inactive_policy_for(quest.project_root)

    monkeypatch.setattr("imp.engine.executor._build_sandbox_policy", _build)
    return builds


async def go_live(inv: AgentInvocation) -> None:
    """Run the production go-live tool against this invocation's quest.

    The scripted/SDK fakes bypass MCP, so a scripted tool_use never reaches
    imp's tool dispatch; invoking the tool directly is the same call the step
    makes.
    """
    from imp.agents.tools import get_registry
    from imp.agents.tools.context import ToolContext

    ctx = ToolContext(project_root=inv.quest.project_root,
                      events_path=inv.quest.events_path)
    ctx.quest = inv.quest
    ctx.quest_state = inv.quest_state
    ctx.base_sandbox_policy = inv.sandbox_policy
    await get_registry()["create_and_enter_worktree"].async_invoke(ctx, {})


class GoLiveOnce:
    """Wraps a runner: the first drive goes live before streaming, later drives
    delegate unchanged. With fail_first, the first drive yields a retryable auth
    error instead of streaming, so the step re-enters through the backoff loop.

    `fired` is shared across every runner the provider mints, so go-live happens
    exactly once — on the step's first drive.
    """

    def __init__(self, inner: AgentRunner, fired: list[bool], *,
                 fail_first: bool = False) -> None:
        self._inner = inner
        self._fired = fired
        self._fail_first = fail_first

    async def run(self, inv: AgentInvocation) -> AsyncIterator[Any]:
        if not self._fired:
            self._fired.append(True)
            await go_live(inv)
            if self._fail_first:
                yield {"type": "error", "payload": {
                    "category": "auth", "kind": "AuthTransientError",
                    "message": "API Error: 401 rejected by gateway"}}
                return
        async for ev in self._inner.run(inv):
            yield ev

    async def interrupt(self) -> None:
        await self._inner.interrupt()
