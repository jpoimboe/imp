"""`_emit_daemon_died` renders the exit block for a run whose stream ended with no
outcome on it: the crash pointer when daemon.stderr holds a traceback, otherwise the
plain statement that nothing was reported."""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.cli import main as cli_main
from imp.cli import palette as pal
from imp.engine.quest import Quest
from tests.conftest import _seed_control


def _quest(tmp_path: Path, title: str = "Add dark mode") -> Quest:
    _seed_control(tmp_path)
    return Quest.create(
        tmp_path, title=title, quest_id="feat", base_branch="main")


def test_daemon_died_with_a_traceback_points_at_the_stderr_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_main, "should_use_color", lambda stream: False)
    quest = _quest(tmp_path)
    stderr_path = quest.quest_dir / "daemon.stderr"
    stderr_path.write_text(
        "Traceback (most recent call last):\n  File \"x.py\", line 1\nRuntimeError\n")
    cli_main._emit_daemon_died(quest)
    out = capsys.readouterr().out
    assert out == (
        f"crash: see {stderr_path}\n"
        "feat · Add dark mode\n"
        "\n")


def test_daemon_died_without_a_traceback_reports_no_outcome(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_main, "should_use_color", lambda stream: False)
    quest = _quest(tmp_path)
    (quest.quest_dir / "daemon.stderr").write_text("INFO: provider chatter\n")
    cli_main._emit_daemon_died(quest)
    out = capsys.readouterr().out
    assert out == (
        "run ended without reporting an outcome\n"
        "It may not have finished — `imp resume feat` to check.\n"
        "feat · Add dark mode\n"
        "\n")
    # The inline hint is the remediation; there is no standalone resume line.
    assert not any(ln.startswith("resume: imp resume") for ln in out.split("\n"))


def test_daemon_died_head_is_red_when_colored_and_carries_the_intent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_main, "should_use_color", lambda stream: True)
    quest = _quest(tmp_path, title="")
    cli_main._emit_daemon_died(quest, intent="add a dark mode toggle")
    out = capsys.readouterr().out
    assert pal.ansi("run ended without reporting an outcome", "red") in out
    assert "add a dark mode toggle" in out       # identity line, prompt fallback
