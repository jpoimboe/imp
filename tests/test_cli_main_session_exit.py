from __future__ import annotations

from types import SimpleNamespace

import pytest

import imp.cli.main as main
from imp.cli.session_run import SettledOutcome


def test_session_exit_code_emits_pause_summary_even_on_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = SettledOutcome(status="paused", events=[])
    monkeypatch.setattr(
        "imp.cli.session_run.resolve_settled_outcome", lambda _c: outcome)
    summarized: list[SettledOutcome] = []
    monkeypatch.setattr(
        main, "_handle_terminal_outcome",
        lambda _c, resolved, _co: summarized.append(resolved) or 1)
    coord = SimpleNamespace(should_pause=False, interrupted=True)
    rc = main._session_exit_code(SimpleNamespace(), coord)
    assert rc == 130                    # interrupt exit code
    assert summarized == [outcome]      # summary path WAS invoked


def test_session_exit_code_returns_outcome_when_not_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = SettledOutcome(status="merged", events=[])
    monkeypatch.setattr(
        "imp.cli.session_run.resolve_settled_outcome", lambda _c: outcome)
    summarized: list[SettledOutcome] = []
    monkeypatch.setattr(
        main, "_handle_terminal_outcome",
        lambda _c, resolved, _co: summarized.append(resolved) or 0)
    coord = SimpleNamespace(should_pause=False, interrupted=False)
    assert main._session_exit_code(SimpleNamespace(), coord) == 0
    assert summarized == [outcome]


def test_session_exit_code_completed_run_ignores_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 130 keys off the same single resolution: a completed run is never an interrupt exit.
    monkeypatch.setattr(
        "imp.cli.session_run.resolve_settled_outcome",
        lambda _c: SettledOutcome(status="merged", events=[]))
    monkeypatch.setattr(main, "_handle_terminal_outcome", lambda *_a: 0)
    coord = SimpleNamespace(should_pause=False, interrupted=True)
    assert main._session_exit_code(SimpleNamespace(), coord) == 0
