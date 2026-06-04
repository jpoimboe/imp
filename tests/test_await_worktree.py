"""Unit test for the shared _await_worktree helper in tests/_live.py.

Uses a real Change worktree (cheap, no providers) so the helper's "find the
single Change worktree dir, return its path; .name is the slug" contract is
exercised against the actual on-disk layout — including ignoring the
transient .merge-* scratch dirs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.change import Change
from tests._live import _await_worktree
from tests.conftest import _seed_control


def test_await_worktree_returns_the_change_dir(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="Hello There", change_id="hello-there", base_branch="main")
    wt = _await_worktree(tmp_path, timeout=5.0)
    assert wt.name == "hello-there"
    assert (wt / ".imp" / "change" / "change.json").exists()


def test_await_worktree_ignores_merge_scratch(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="Real One", change_id="real-one", base_branch="main")
    # A scratch dir without change.json must be ignored even if it sorts first.
    scratch = tmp_path / ".imp" / "worktrees" / ".merge-aaa"
    scratch.mkdir(parents=True)
    wt = _await_worktree(tmp_path, timeout=5.0)
    assert wt.name == "real-one"


def test_await_worktree_times_out(tmp_path: Path) -> None:
    (tmp_path / ".imp" / "worktrees").mkdir(parents=True)
    with pytest.raises(TimeoutError):
        _await_worktree(tmp_path, timeout=0.5)
