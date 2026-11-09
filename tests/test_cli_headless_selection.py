"""Headless selection: resume --message, no-TTY auto, and the hidden compat flag."""
from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

import imp.cli.main as m
import imp.cli.signals as sig
from imp.engine.quest import Quest
from imp.engine.state import State
from tests.conftest import _seed_control

_NOTICE = "imp: no tty detected — running headless; resume with `imp resume feat`\n"


def _stream(*, tty: bool) -> SimpleNamespace:
    return SimpleNamespace(isatty=lambda: tty)


def _on(monkeypatch: pytest.MonkeyPatch, *, tty: bool) -> None:
    real = m._select_headless
    monkeypatch.setattr(m, "_select_headless", lambda args: real(
        args, stdin=_stream(tty=tty), stdout=_stream(tty=tty)))


@pytest.fixture
def runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Records which resume path ran: headless (with its solicit message) or interactive."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("IMP_SESSION_SOCKET", raising=False)
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(m, "should_use_color", lambda *a, **k: False)
    monkeypatch.setattr("imp.cli.live_status.LiveStatus", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(m, "_build_resume_driver", lambda **k: SimpleNamespace())
    monkeypatch.setattr(sig, "InterruptCoordinator", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("imp.cli.session_run.make_headless_solicit",
                        lambda message, coord: seen.append({"solicit": message}))

    async def _headless(quest: Quest, **kw: Any) -> int:
        seen.append({"path": "headless", **kw})
        return 0

    async def _interactive(*a: Any, **k: Any) -> int:
        seen.append({"path": "interactive"})
        return 0

    monkeypatch.setattr("imp.cli.session_run.run_session_client", _headless)
    monkeypatch.setattr(m, "_resume_inert", _interactive)
    monkeypatch.setattr(m, "_clear_loop", _interactive)
    return seen


def _paused(tmp_path: Path) -> Quest:
    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="demo", quest_id="feat", base_branch="main")
    state = State.load(quest.state_dir)
    state.status = "paused"
    state.has_started = True
    state.save(quest.state_dir)
    return quest


def _resume(*argv: str) -> int:
    return anyio.run(m._async_cmd_resume, m._build_parser().parse_args(["resume", *argv]))


def _path(runs: list[dict[str, Any]]) -> str:
    (run,) = [r for r in runs if "path" in r]
    return run["path"]


def _help(argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit):
        m._build_parser().parse_args([*argv, "-h"])
    return capsys.readouterr().out


def _action(cmd: str, dest: str) -> argparse.Action:
    parser = m._build_parser()
    sub = next(a for a in parser._actions
               if getattr(a, "choices", None) and cmd in a.choices)
    return next(a for a in sub.choices[cmd]._actions if a.dest == dest)


@pytest.mark.parametrize("cmd", ["new", "chat", "resume"])
def test_help_lists_no_headless(cmd: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert "headless" not in _help([cmd], capsys).lower()


def test_chat_autonomous_and_resume_message_help_use_the_approved_wording() -> None:
    assert _action("chat", "autonomous").help == (
        "Take imp's recommendation at routine decision points instead of asking "
        "you; questions only you can answer still pause for you.")
    assert _action("resume", "message").help == (
        "Reply to the question the quest is waiting on (or the next one it asks), "
        "with no live prompt; answer a later question by resuming again with "
        "another --message.")


@pytest.mark.parametrize("argv", [
    ["new", "--headless"],
    ["chat", "--headless"],
    ["new", "--backlog", "T", "--headless"],
])
def test_new_and_chat_reject_headless(
    argv: list[str], capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        m._build_parser().parse_args(argv)
    assert exc.value.code == 2
    assert "unrecognized arguments: --headless" in capsys.readouterr().err


@pytest.mark.parametrize("cmd", ["new", "chat", "resume"])
def test_the_old_non_interactive_spelling_stays_rejected(cmd: str) -> None:
    with pytest.raises(SystemExit):
        m._build_parser().parse_args([cmd, "--non-interactive"])


@pytest.mark.parametrize("cmd", ["new", "chat"])
def test_new_and_chat_message_stay_interactive_on_a_tty(cmd: str) -> None:
    args = m._build_parser().parse_args([cmd, "--message", "X"])
    assert m._select_headless(
        args, stdin=_stream(tty=True), stdout=_stream(tty=True)) == (False, False)


@pytest.mark.parametrize("cmd", ["new", "chat"])
def test_new_and_chat_with_no_tty_go_headless_automatically(cmd: str) -> None:
    args = m._build_parser().parse_args([cmd, "x"])
    assert m._select_headless(
        args, stdin=_stream(tty=False), stdout=_stream(tty=False)) == (True, True)


def test_resume_message_on_a_tty_runs_headless_and_delivers_the_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runs: list[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _paused(tmp_path)
    _on(monkeypatch, tty=True)

    assert _resume("feat", "--message", "ship it") == 0

    assert _path(runs) == "headless"
    assert {"solicit": "ship it"} in runs
    assert "no tty detected" not in capsys.readouterr().err


def test_resume_without_message_on_a_tty_stays_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runs: list[dict[str, Any]],
) -> None:
    _paused(tmp_path)
    _on(monkeypatch, tty=True)

    assert _resume("feat") == 0

    assert _path(runs) == "interactive"


def test_resume_message_on_a_fresh_draft_runs_the_fresh_session_headless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runs: list[dict[str, Any]],
) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="demo", quest_id="seed1", base_branch="main",
                       seed_opening="do the thing")
    _on(monkeypatch, tty=True)
    selected: list[tuple[bool, bool]] = []

    async def _fresh(quest: Quest, *, args: argparse.Namespace) -> int:
        selected.append(m._select_headless(args))
        return 0

    monkeypatch.setattr(m, "_run_fresh_session", _fresh)

    assert _resume("seed1", "--message", "go") == 0

    assert selected == [(True, False)]


def test_resume_with_no_tty_goes_headless_with_the_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runs: list[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _paused(tmp_path)
    _on(monkeypatch, tty=False)

    assert _resume("feat") == 0

    assert _path(runs) == "headless"
    assert _NOTICE in capsys.readouterr().err


def test_resume_message_with_no_tty_omits_the_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runs: list[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _paused(tmp_path)
    _on(monkeypatch, tty=False)

    assert _resume("feat", "--message", "go") == 0

    assert _path(runs) == "headless"
    assert "no tty detected" not in capsys.readouterr().err


@pytest.mark.parametrize(("extra", "autonomous"), [
    (["--autonomous"], True),
    ([], False),
])
def test_pre_merge_spawner_argv_still_runs_headless_with_no_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runs: list[dict[str, Any]],
    extra: list[str], autonomous: bool,
) -> None:
    _paused(tmp_path)
    _on(monkeypatch, tty=False)

    assert _resume("feat", "--headless", *extra) == 0

    (run,) = [r for r in runs if "path" in r]
    assert (run["path"], run["non_interactive"], run["autonomous"]) == (
        "headless", True, autonomous)
    assert {"solicit": None} in runs


def test_the_hidden_headless_flag_is_ignored_on_a_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runs: list[dict[str, Any]],
) -> None:
    _paused(tmp_path)
    _on(monkeypatch, tty=True)

    assert _resume("feat", "--headless") == 0

    assert _path(runs) == "interactive"
