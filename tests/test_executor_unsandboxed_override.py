from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from imp.agents.tools.context import ToolContext
from imp.engine.sandbox import SandboxPolicy


def _active_policy() -> SandboxPolicy:
    return SandboxPolicy(
        active=True, worktree="/wt", git_common_dir="/wt/.git",
        git_ro_remask=(), writable_caches=(), extra_readable=(),
        extra_writable=(), secret_dirs=(), secret_files=(), child_env=(),
        network=True)


def _ctx(base, coordinator) -> ToolContext:
    ctx = ToolContext(project_root=Path("/wt"), events_path=Path("/wt/events.jsonl"))
    ctx.base_sandbox_policy = base
    ctx.coordinator = coordinator
    return ctx


def test_override_deactivates_policy_live() -> None:
    ctx = _ctx(_active_policy(), SimpleNamespace(unsandboxed=True))
    assert ctx.sandbox_policy.active is False
    assert ctx.sandbox_policy.note == "unsandboxed_override"


def test_without_override_returns_active_base() -> None:
    base = _active_policy()
    ctx = _ctx(base, SimpleNamespace(unsandboxed=False))
    assert ctx.sandbox_policy is base
    assert ctx.sandbox_policy.active is True


def test_none_base_reads_none() -> None:
    ctx = _ctx(None, SimpleNamespace(unsandboxed=True))
    assert ctx.sandbox_policy is None


def test_none_coordinator_returns_base() -> None:
    base = _active_policy()
    ctx = _ctx(base, None)
    assert ctx.sandbox_policy is base
