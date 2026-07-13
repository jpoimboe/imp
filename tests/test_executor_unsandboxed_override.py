from __future__ import annotations

from types import SimpleNamespace

from imp.engine.executor import _apply_unsandboxed_override
from imp.engine.sandbox import SandboxPolicy


def _active_policy() -> SandboxPolicy:
    return SandboxPolicy(
        active=True, worktree="/wt", git_common_dir="/wt/.git",
        git_ro_remask=(), writable_caches=(), extra_readable=(),
        extra_writable=(), secret_dirs=(), secret_files=(), setenv=(),
        network=True)


def _step(*, interactive: bool) -> SimpleNamespace:
    return SimpleNamespace(interactive=interactive)


def test_interactive_step_with_override_deactivates_policy() -> None:
    coord = SimpleNamespace(unsandboxed=True)
    out = _apply_unsandboxed_override(_active_policy(), _step(interactive=True), coord)
    assert out.active is False
    assert out.note == "unsandboxed_override"


def test_interactive_step_without_override_is_unchanged() -> None:
    coord = SimpleNamespace(unsandboxed=False)
    policy = _active_policy()
    out = _apply_unsandboxed_override(policy, _step(interactive=True), coord)
    assert out.active is True
    assert out is policy


def test_non_interactive_step_honors_the_override() -> None:
    # Step-scoped: with the interactive gate removed, a non-interactive phase
    # step is deactivated when the session override is set (acceptance criterion:
    # non-interactive-step-honors-override).
    coord = SimpleNamespace(unsandboxed=True)
    policy = _active_policy()
    out = _apply_unsandboxed_override(policy, _step(interactive=False), coord)
    assert out.active is False
    assert out.note == "unsandboxed_override"


def test_none_policy_and_none_coordinator_are_safe() -> None:
    assert _apply_unsandboxed_override(None, _step(interactive=True),
                                       SimpleNamespace(unsandboxed=True)) is None
    policy = _active_policy()
    assert _apply_unsandboxed_override(policy, _step(interactive=True), None) is policy
