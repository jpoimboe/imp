"""The keeper namespace hung off a ToolContext: lazy open, reuse, rebuild, and the
cross-call guarantees an agent run sees.

Real keepers driven through the real bash tool, asserting the OS-level outcome.
Helpers come from tests.test_sandbox_namespace so the two files cannot drift.
"""
from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from imp.agents.runner import QuestContext
from imp.agents.tools.context import ToolContext, ensure_tool_namespace
from imp.engine.sandbox import sandbox_available
from tests.conftest import own_tool_context
from tests.test_sandbox_namespace import _make_root, _policy_for

# The module timeout is the SINGLE outer backstop for a genuine hang; it exceeds
# the sum of the inner budgets any one test can spend.
pytestmark = [
    pytest.mark.skipif(not sandbox_available(), reason="bwrap unavailable"),
    pytest.mark.timeout(300),
]


def _ctx(root: Path) -> ToolContext:
    ctx = ToolContext(project_root=root, events_path=root / "events.jsonl")
    ctx.quest_context = QuestContext(
        id="test01", title="Test Quest", project_root=root,
        workflow_dir=root / ".imp" / "wf")
    ctx.base_sandbox_policy = _policy_for(root)
    # A real coordinator's only surface here: bash reads should_pause every 50ms
    # and ctx.sandbox_policy reads unsandboxed on every access (the /sandbox toggle).
    ctx.coordinator = SimpleNamespace(unsandboxed=False, should_pause=False)
    ctx.agent_id = "step:build/coder"
    return own_tool_context(ctx)


def _open(ctx: ToolContext):
    return anyio.run(lambda: ensure_tool_namespace(
        ctx, engine_env={}, on_retry=lambda _n, _d: None))


def test_the_namespace_opens_once_and_is_reused():
    # Lazy open on first use, then reuse: a second call must not spawn a second
    # keeper, or every call would pay the spawn AND lose the previous call's
    # processes.
    root = _make_root()
    try:
        ctx = _ctx(root)
        assert ctx.sandbox_ns is None            # nothing paid before first use
        first = _open(ctx)
        assert _open(ctx) is first
        assert ctx.sandbox_ns is first
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_changed_base_policy_rebuilds_the_namespace():
    # create_and_enter_worktree reassigns ctx.base_sandbox_policy mid-run
    # (create_and_enter_worktree.py:126-131), and a keeper pinned to the
    # pre-materialize bind plan could not see the worktree the agent was just told
    # it is in. Keyed on the value, so no caller has to remember a hook.
    root = _make_root()
    other = _make_root()
    try:
        ctx = _ctx(root)
        first = _open(ctx)
        ctx.base_sandbox_policy = dataclasses.replace(
            ctx.base_sandbox_policy, worktree=str(other))
        second = _open(ctx)
        assert second is not first
        assert first.closed                      # the old one is closed, not leaked
        assert second.base_policy is ctx.base_sandbox_policy
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(other, ignore_errors=True)


def test_an_unchanged_policy_value_is_not_a_rebuild():
    # The key is the VALUE, not the identity: rebuilding on an equal-but-new
    # object would kill the agent's own background work for nothing.
    root = _make_root()
    try:
        ctx = _ctx(root)
        first = _open(ctx)
        ctx.base_sandbox_policy = dataclasses.replace(ctx.base_sandbox_policy)
        assert _open(ctx) is first
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_keeper_survives_the_run_loop_that_opened_it():
    # --die-with-parent is PR_SET_PDEATHSIG, which the kernel fires when the
    # CREATING THREAD exits. Opening in a worker thread therefore kills the keeper
    # the moment that worker retires; this pins that open() runs on the caller's
    # own thread, across the anyio.run boundary every tool call crosses.
    root = _make_root()
    try:
        ctx = _ctx(root)
        ns = _open(ctx)                          # one anyio.run, now finished
        anyio.run(anyio.sleep, 0.01)             # and another, opening nothing
        spawn = ns.join("echo alive", cwd=str(root), engine_env={})
        done = subprocess.run(spawn.argv, env=spawn.env, pass_fds=spawn.pass_fds,
                              capture_output=True, text=True, timeout=30.0)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "alive"
    finally:
        shutil.rmtree(root, ignore_errors=True)
