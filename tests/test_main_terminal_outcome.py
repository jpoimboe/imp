from __future__ import annotations

from unittest.mock import patch

from imp.cli import main
from imp.engine.executor import RunResult


class _Events:
    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def read(self) -> list[dict]:
        return list(self._events)


class _Change:
    id = "2026-06-12-run-x"
    base_branch = "master"
    state_dir = "/tmp/imp-test-state"   # never touched: State.load is patched

    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def event_log(self) -> _Events:
        return _Events(self._events)


class _State:
    def __init__(self, status: str, step: str | None = None) -> None:
        self.status = status
        self.step = step


def test_merge_blocked_outcome_emits_no_paused_summary() -> None:
    change = _Change([{"type": "merge_blocked",
                       "payload": {"reason": "refused", "base": "master",
                                   "detail": "error: Aborting"}}])
    with patch.object(main, "State") as State, \
         patch.object(main, "_emit_merge_blocked") as mb, \
         patch.object(main, "_emit_paused_summary") as ps:
        State.load.return_value = _State("merge_blocked")
        rc = main._handle_terminal_outcome(change, RunResult.PAUSED, coord=None)
    assert rc == 1
    mb.assert_called_once()          # the friendly ⚠ block still prints
    ps.assert_not_called()           # the redundant second pause line is gone


def test_non_merge_pause_still_emits_paused_summary() -> None:
    change = _Change([])
    with patch.object(main, "State") as State, \
         patch.object(main, "_emit_paused_summary") as ps:
        State.load.return_value = _State("paused", step=None)
        rc = main._handle_terminal_outcome(change, RunResult.PAUSED, coord=None)
    assert rc == 1
    ps.assert_called_once()          # ordinary pauses keep the summary line
