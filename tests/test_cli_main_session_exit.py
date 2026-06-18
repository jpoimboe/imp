from __future__ import annotations

from types import SimpleNamespace

import pytest

import imp.cli.main as main
from imp.engine.executor import RunResult


def test_session_exit_code_emits_pause_summary_even_on_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "infer_session_result", lambda _c: RunResult.PAUSED)
    summarized: list[RunResult] = []
    monkeypatch.setattr(
        main, "_handle_terminal_outcome",
        lambda _c, result, _co: summarized.append(result) or 1)
    coord = SimpleNamespace(should_pause=True)
    rc = main._session_exit_code(SimpleNamespace(), coord)
    assert rc == 130                       # interrupt exit code
    assert summarized == [RunResult.PAUSED]  # summary path WAS invoked


def test_session_exit_code_returns_outcome_when_not_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "infer_session_result", lambda _c: RunResult.COMPLETED)
    summarized: list[RunResult] = []
    monkeypatch.setattr(
        main, "_handle_terminal_outcome",
        lambda _c, result, _co: summarized.append(result) or 0)
    coord = SimpleNamespace(should_pause=False)
    assert main._session_exit_code(SimpleNamespace(), coord) == 0
    assert summarized == [RunResult.COMPLETED]
