"""`_emit_lost_connection` renders the shared three-part exit block when the CLI's
event stream ended with no end-of-run frame: the run's outcome was never observed,
so the block says so instead of guessing a state from disk (zake22). Its
reachability from a CUT ending is covered in tests/test_session_run.py."""
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


def test_lost_connection_renders_the_intent_it_is_handed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_main, "should_use_color", lambda stream: False)
    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="", quest_id="lc9", base_branch="main")
    quest.event_log().append(
        agent_id=None, type="user_message",
        payload={"subtype": "intent",
                 "content": [{"type": "text", "text": "the log's own intent"}]})
    cli_main._emit_lost_connection(quest, intent="witnessed intent")
    out = capsys.readouterr().out
    assert "lc9 · witnessed intent" in out
    assert "the log's own intent" not in out
