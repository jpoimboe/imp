from __future__ import annotations

from imp.engine.change import Change
from imp.engine.executor import RunResult
from imp.engine.state import State


def infer_session_result(change: Change) -> RunResult:
    """Infer RunResult from disk: no worktree or completed status → COMPLETED, else PAUSED."""
    worktree = change.control_root / ".imp" / "worktrees" / change.id
    if not worktree.exists():
        return RunResult.COMPLETED
    if State.load(change.state_dir).status == "completed":
        return RunResult.COMPLETED
    return RunResult.PAUSED
