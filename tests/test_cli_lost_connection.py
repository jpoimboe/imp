"""`_emit_lost_connection` renders the shared three-part exit block when the CLI's
event stream faulted: the run's outcome was never observed, so the block says so
instead of guessing a state from disk (zake22)."""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.cli import main as cli_main
from imp.cli import palette as pal
from imp.engine.quest import Quest
from tests.conftest import _seed_control


def _quest(tmp_path: Path) -> Quest:
    _seed_control(tmp_path)
    return Quest.create(
        tmp_path, title="Add dark mode", quest_id="feat", base_branch="main")


def test_lost_connection_block_is_exactly_three_lines_plus_one_blank(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_main, "should_use_color", lambda stream: False)
    cli_main._emit_lost_connection(_quest(tmp_path))
    out = capsys.readouterr().out
    assert out == (
        "lost the connection to the run\n"
        "It may still be running — `imp resume feat` to reattach.\n"
        "feat · Add dark mode\n"
        "\n")


def test_lost_connection_block_never_offers_a_pause_or_resume_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_main, "should_use_color", lambda stream: False)
    cli_main._emit_lost_connection(_quest(tmp_path))
    out = capsys.readouterr().out
    assert "paused" not in out
    assert "resume: imp resume" not in out   # the inline hint is the remediation


def test_lost_connection_head_is_red_when_colored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_main, "should_use_color", lambda stream: True)
    cli_main._emit_lost_connection(_quest(tmp_path))
    out = capsys.readouterr().out
    assert pal.ansi("lost the connection to the run", "red") in out
    assert "feat" in out                     # gold identity line
