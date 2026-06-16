from __future__ import annotations

from pathlib import Path

from imp.cli.session_run import infer_session_result
from imp.engine.change import Change
from imp.engine.executor import RunResult
from imp.engine.state import State


def _make_change(tmp_path: Path, change_id: str) -> Change:
    return Change(
        id=change_id,
        title="T",
        base_branch="main",
        control_root=tmp_path,
        project_root=tmp_path,
        change_dir=tmp_path / ".imp" / "change",
    )


def test_infer_session_result_completed_when_worktree_removed(tmp_path: Path) -> None:
    # A merge-completing run removes the worktree (and the state dir inside it).
    change = _make_change(tmp_path, "2026-06-15-merged")
    assert infer_session_result(change) is RunResult.COMPLETED


def test_infer_session_result_completed_when_status_completed(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-done")
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step=None, step_attempt=0, status="completed").save(
        change.state_dir)
    assert infer_session_result(change) is RunResult.COMPLETED


def test_infer_session_result_paused_when_parked_at_gate(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-parked")
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step="plan", step_attempt=0, status="paused").save(
        change.state_dir)
    assert infer_session_result(change) is RunResult.PAUSED


def test_infer_session_result_paused_when_merge_blocked(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-blocked")
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step=None, step_attempt=0, status="merge_blocked").save(
        change.state_dir)
    assert infer_session_result(change) is RunResult.PAUSED
