"""`_emit_startup_failed` renders the shared three-part exit block on stdout for a
daemon that died before binding its socket. It builds from DaemonStartupError's
structured fields (exit_code, stderr_path, stderr_tail), degrading to a backstop
head+body when no error detail is available. This replaces the old ad-hoc
`imp: …` stderr line so a startup failure reads like every other terminal exit
block (sajo68)."""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.cli import main as cli_main
from imp.cli import palette as pal
from imp.engine.quest import Quest
from imp.paths import project_state_dir
from imp.remote.session.attach import DaemonStartupError
from tests.conftest import _seed_control


def _quest(tmp_path: Path) -> Quest:
    _seed_control(tmp_path)
    return Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")


def test_daemon_error_with_exit_code_plain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_main, "should_use_color", lambda stream: False)
    q = _quest(tmp_path)
    stderr_path = tmp_path / "daemon.stderr"
    err = DaemonStartupError("feat", 7, stderr_path, "RuntimeError: boom-on-startup")
    cli_main._emit_startup_failed(q, err)
    out = capsys.readouterr().out
    assert "startup failed: session daemon exited with code 7 before it came up" in out
    assert "RuntimeError: boom-on-startup" in out          # captured stderr tail
    assert f"see {stderr_path}" in out                      # pointer to the log
    assert "feat" in out                                    # gold identity line
    assert "\x1b[" not in out                               # plain under NO_COLOR
    assert out.endswith("\n\n")                             # one trailing blank
    assert not out.endswith("\n\n\n")
    lines = out.rstrip("\n").split("\n")
    assert lines.index("RuntimeError: boom-on-startup") < lines.index(f"see {stderr_path}")


def test_daemon_error_with_exit_code_colored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_main, "should_use_color", lambda stream: True)
    q = _quest(tmp_path)
    err = DaemonStartupError("feat", 7, tmp_path / "daemon.stderr", "boom")
    cli_main._emit_startup_failed(q, err)
    out = capsys.readouterr().out
    assert pal.ansi(
        "startup failed: session daemon exited with code 7 before it came up",
        "red") in out                                       # red head
    assert "feat" in out


def test_daemon_error_no_exit_code_uses_generic_head(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_main, "should_use_color", lambda stream: False)
    q = _quest(tmp_path)
    stderr_path = tmp_path / "daemon.stderr"
    err = DaemonStartupError("feat", None, stderr_path, "boom")
    cli_main._emit_startup_failed(q, err)
    out = capsys.readouterr().out
    assert "startup failed: session daemon exited before it came up" in out
    assert "code" not in out.split("\n")[0]                 # no `code None` in the head
    assert f"see {stderr_path}" in out


def test_daemon_error_empty_tail_omits_tail_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_main, "should_use_color", lambda stream: False)
    q = _quest(tmp_path)
    stderr_path = tmp_path / "daemon.stderr"
    err = DaemonStartupError("feat", 7, stderr_path, "")   # no captured tail
    cli_main._emit_startup_failed(q, err)
    out = capsys.readouterr().out
    assert f"see {stderr_path}" in out
    # Body is exactly the `see` pointer — no empty tail line before it.
    lines = out.rstrip("\n").split("\n")
    assert lines[1] == f"see {stderr_path}"


def test_backstop_plain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_main, "should_use_color", lambda stream: False)
    q = _quest(tmp_path)
    cli_main._emit_startup_failed(q, None)
    out = capsys.readouterr().out
    assert "startup failed: session daemon did not come up" in out
    assert ("It may have crashed; see "
            f"{project_state_dir(q.control_root) / 'remote-control.log'}") in out
    assert "feat" in out
    assert "\x1b[" not in out
    assert out.endswith("\n\n")


def test_backstop_colored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_main, "should_use_color", lambda stream: True)
    q = _quest(tmp_path)
    cli_main._emit_startup_failed(q, None)
    out = capsys.readouterr().out
    assert pal.ansi(
        "startup failed: session daemon did not come up", "red") in out


def _titleless(tmp_path: Path) -> Quest:
    _seed_control(tmp_path)
    return Quest.create(tmp_path, title="", quest_id="feat", base_branch="main")


def test_titleless_first_run_death_is_named_by_seed_opening(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The daemon died before the intent event was written: seed_opening names it.
    monkeypatch.setattr(cli_main, "should_use_color", lambda stream: False)
    q = _titleless(tmp_path)
    q.set_seed_opening("summarize foo.py")
    cli_main._emit_startup_failed(q, DaemonStartupError("feat", 7, tmp_path / "e", ""))
    lines = capsys.readouterr().out.rstrip("\n").split("\n")
    assert lines[-1] == "feat · summarize foo.py"


def test_startup_failed_events_intent_wins_over_seed_opening(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_main, "should_use_color", lambda stream: False)
    q = _titleless(tmp_path)
    q.set_seed_opening("the seeded brief")
    q.event_log().append(
        agent_id=None, type="user_message",
        payload={"subtype": "intent",
                 "content": [{"type": "text", "text": "the log's own intent"}]})
    cli_main._emit_startup_failed(q, DaemonStartupError("feat", 7, tmp_path / "e", ""))
    lines = capsys.readouterr().out.rstrip("\n").split("\n")
    assert lines[-1] == "feat · the log's own intent"
