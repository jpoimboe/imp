from __future__ import annotations

import dataclasses
from pathlib import Path

from imp.engine.executor import _apply_readonly_overlay
from imp.engine.sandbox import SandboxPolicy
from imp.engine.types import SourceLoc, Step


def _active_policy(worktree: Path) -> SandboxPolicy:
    return SandboxPolicy(
        active=True, worktree=str(worktree), git_common_dir=str(worktree / ".git"),
        git_ro_remask=(), writable_caches=(), extra_readable=(), extra_writable=(),
        secret_dirs=(), secret_files=(), setenv=(), network=True, note="",
    )


def _agent_step(read_only: tuple[str, ...]) -> Step:
    return Step(
        name="s", phase="p", kind="agent", rules=(), on_max_retries=None,
        max_loops=1, source=SourceLoc(file=Path("t.yaml"), line=1),
        file=Path("agents/s.md"), read_only=read_only,
    )


def test_apply_readonly_overlay_resolves_relative_to_project_root(tmp_path: Path):
    pol = _active_policy(tmp_path)
    out = _apply_readonly_overlay(pol, _agent_step((".", "vendor")), tmp_path)
    assert out.readonly_overlays == (
        str(tmp_path.resolve()), str((tmp_path / "vendor").resolve()))
    # A whole-worktree overlay locks the worktree but the base policy is untouched.
    assert out.allows_write(tmp_path / "src" / "x.py") is False
    assert pol.readonly_overlays == ()                  # base policy unmutated


def test_apply_readonly_overlay_noop_without_read_only(tmp_path: Path):
    pol = _active_policy(tmp_path)
    assert _apply_readonly_overlay(pol, _agent_step(()), tmp_path) is pol


def test_apply_readonly_overlay_noop_when_policy_none(tmp_path: Path):
    assert _apply_readonly_overlay(None, _agent_step((".",)), tmp_path) is None


def test_apply_readonly_overlay_is_additive_preserves_snapshot(tmp_path):
    snapshot = str(tmp_path / "changes" / "abc" / "workflow")
    base = dataclasses.replace(
        _active_policy(tmp_path), readonly_overlays=(snapshot,))
    out = _apply_readonly_overlay(base, _agent_step((".",)), tmp_path)
    # The step's "." overlay is APPENDED to the pre-existing snapshot overlay,
    # not replacing it — both are present, snapshot first.
    assert out.readonly_overlays == (snapshot, str(tmp_path.resolve()))
    # And the snapshot is still enforced as read-only after composition.
    assert out.allows_write(tmp_path / "changes" / "abc" / "workflow" / "workflow.yaml") is False
