"""Every closing block on a real terminal has exactly one blank row above it."""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from imp.paths import project_state_dir
from impty import TerminalSession
from tests.impty.e2e_support import (
    E2E_EXIT_POLL_TIMEOUT,
    E2E_POLL_TIMEOUT,
    _event_types,
    _flock_free,
    _poll,
    can_pty,
)
from tests.impty.test_e2e_leave_exit_codes import (
    _BUSY_TURNS,
    _CHAT_TURNS,
    _QUESTION_TURNS,
    _assert_closing_layout,
    _assert_ctrl_d_block,
    _assert_pause_block,
    _at_the_question,
    _await_quest_dir,
    _env,
)
from tests.impty.test_e2e_relay_hibernation_gate import (
    _seed_scripted_gate_project,
)

pytestmark = pytest.mark.skipif(not can_pty(), reason="pty not available")

_SIGNALS = [(signal.SIGTERM, "SIGTERM"), (signal.SIGHUP, "SIGHUP"), (signal.SIGQUIT, "SIGQUIT")]
_PLACEHOLDER = "type a message, or /help"
_HINT = "press Ctrl-D to pause & exit"

def _session(tmp_path: Path, turns: list[dict[str, Any]], title: str, cols: int = 80,
             ) -> TerminalSession:
    script = _seed_scripted_gate_project(tmp_path, turns=turns)
    return TerminalSession(
        [sys.executable, "-m", "imp", "new", title, "--title", title],
        cwd=str(tmp_path), env=_env(script), cols=cols, rows=24)


def _screen(s: TerminalSession) -> str:
    return "\n".join(s.lines())


def _at_empty_prompt(s: TerminalSession) -> None:
    assert _poll(lambda: any(_PLACEHOLDER in ln for ln in s.lines()),
                 timeout=E2E_POLL_TIMEOUT), _screen(s)


def _send_and_see(s: TerminalSession, keys: str, row: str) -> None:
    s.send(keys)
    assert _poll(lambda: any(row in ln for ln in s.lines()),
                 timeout=E2E_POLL_TIMEOUT), _screen(s)


@pytest.mark.parametrize(("sig", "display"), _SIGNALS, ids=[d for _, d in _SIGNALS])
def test_signal_at_a_prompt_renders_the_closing_layout(
    tmp_path: Path, sig: signal.Signals, display: str,
) -> None:
    with _session(tmp_path, _QUESTION_TURNS, "signal prompt") as s:
        quest_dir = _at_the_question(s, tmp_path)
        s.send_signal(sig)
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

        assert rc == 130, _screen(s)
        _assert_pause_block(s, f"paused · {display}", quest_dir.name, "signal prompt")


@pytest.mark.parametrize(("sig", "display"), _SIGNALS, ids=[d for _, d in _SIGNALS])
def test_signal_mid_run_renders_the_closing_layout(
    tmp_path: Path, sig: signal.Signals, display: str,
) -> None:
    with _session(tmp_path, _BUSY_TURNS, "signal mid run") as s:
        quest_dir = _await_quest_dir(tmp_path, timeout=E2E_POLL_TIMEOUT)
        assert _poll(lambda: (quest_dir / "busy").exists(), timeout=E2E_POLL_TIMEOUT), _screen(s)
        s.send_signal(sig)
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

        assert rc == 130, _screen(s)
        _assert_pause_block(s, f"paused · {display}", quest_dir.name, "signal mid run")


def test_abandon_confirmed_keeps_the_answer_row_above_the_blank(tmp_path: Path) -> None:
    with _session(tmp_path, _QUESTION_TURNS, "abandon gate", cols=120) as s:
        quest_dir = _at_the_question(s, tmp_path)
        _at_empty_prompt(s)
        _send_and_see(s, "/abandon\r", "[y/N]")
        s.send("y")
        s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

        rows = s.lines()
        at = _assert_closing_layout(rows, f"{quest_dir.name} · abandon gate · abandoned")
        assert rows[at - 2].endswith("[y/N] y"), _screen(s)


def test_ctrl_c_hint_then_ctrl_d_keeps_the_hint_above_the_blank(tmp_path: Path) -> None:
    with _session(tmp_path, _QUESTION_TURNS, "hint gate") as s:
        quest_dir = _at_the_question(s, tmp_path)
        _at_empty_prompt(s)
        _send_and_see(s, "\x03", _HINT)
        s.send("\x04")
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

        assert rc == 130, _screen(s)
        at = _assert_ctrl_d_block(s, quest_dir.name, "hint gate")
        assert s.lines()[at - 2] == _HINT, _screen(s)


def test_ctrl_c_hint_then_sigterm_keeps_the_hint_above_the_blank(tmp_path: Path) -> None:
    with _session(tmp_path, _QUESTION_TURNS, "hint gate") as s:
        quest_dir = _at_the_question(s, tmp_path)
        _at_empty_prompt(s)
        _send_and_see(s, "\x03", _HINT)
        s.send_signal(signal.SIGTERM)
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

        assert rc == 130, _screen(s)
        at = _assert_pause_block(s, "paused · SIGTERM", quest_dir.name, "hint gate")
        assert s.lines()[at - 2] == _HINT, _screen(s)


def test_ctrl_c_hint_leaves_a_blank_row_above_the_redrawn_prompt(tmp_path: Path) -> None:
    with _session(tmp_path, _QUESTION_TURNS, "hint gap") as s:
        _at_the_question(s, tmp_path)
        _at_empty_prompt(s)
        s.send("\x03")

        def _prompt_redrawn_under_hint() -> bool:
            rows = s.lines()
            if _HINT not in rows:
                return False
            at = rows.index(_HINT)
            return any(_PLACEHOLDER in row for row in rows[at + 1:])

        assert _poll(_prompt_redrawn_under_hint, timeout=E2E_POLL_TIMEOUT), _screen(s)
        rows = s.lines()
        assert rows[rows.index(_HINT) + 1] == "", _screen(s)
        s.send("\x04")
        assert s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT) == 130, _screen(s)


def test_help_reprompt_then_ctrl_d_renders_the_closing_layout(tmp_path: Path) -> None:
    with _session(tmp_path, _QUESTION_TURNS, "help gate") as s:
        quest_dir = _at_the_question(s, tmp_path)
        _at_empty_prompt(s)
        _send_and_see(s, "/help\r", "Complete a /command")
        _at_empty_prompt(s)
        s.send("\x04")
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

        assert rc == 130, _screen(s)
        at = _assert_ctrl_d_block(s, quest_dir.name, "help gate")
        assert "Complete a /command" in s.lines()[at - 2], _screen(s)


def test_declined_abandon_then_help_then_ctrl_d_renders_the_closing_layout(
    tmp_path: Path,
) -> None:
    with _session(tmp_path, _QUESTION_TURNS, "kept gate", cols=100) as s:
        quest_dir = _at_the_question(s, tmp_path)
        _at_empty_prompt(s)
        _send_and_see(s, "/abandon\r", "[y/N]")
        _send_and_see(s, "n", f"Kept {quest_dir.name}")
        _at_empty_prompt(s)
        _send_and_see(s, "/help\r", "Complete a /command")
        _at_empty_prompt(s)
        s.send("\x04")
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

        assert rc == 130, _screen(s)
        at = _assert_ctrl_d_block(s, quest_dir.name, "kept gate")
        assert "Complete a /command" in s.lines()[at - 2], _screen(s)


def test_sigterm_with_stdout_piped_writes_no_row_above_the_block(tmp_path: Path) -> None:
    script = _seed_scripted_gate_project(tmp_path, turns=_BUSY_TURNS)
    out_path = tmp_path / "stdout.txt"
    with out_path.open("w") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "imp", "new", "piped", "--title", "piped"],
            cwd=tmp_path, env=_env(script), stdin=subprocess.DEVNULL, stdout=out,
            stderr=subprocess.STDOUT)
        try:
            quest_dir = _await_quest_dir(tmp_path, timeout=E2E_POLL_TIMEOUT)
            assert _poll(lambda: (quest_dir / "busy").exists(), timeout=E2E_POLL_TIMEOUT), (
                out_path.read_text())
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=E2E_EXIT_POLL_TIMEOUT) == 130, out_path.read_text()
        finally:
            proc.kill()
            proc.wait()

    rows = out_path.read_text().splitlines()
    spine = f"{quest_dir.name} · piped · paused · SIGTERM"
    assert spine in rows, "\n".join(rows)
    at = rows.index(spine)
    assert at >= 1, "\n".join(rows)
    assert rows[at - 1] != "", "\n".join(rows)
    assert rows[at + 1] == f"resume: imp resume {quest_dir.name}", "\n".join(rows)


def test_live_resume_has_one_blank_above_its_first_prompt_and_its_closing_block(
    tmp_path: Path,
) -> None:
    script = _seed_scripted_gate_project(tmp_path, turns=_BUSY_TURNS, interrupt="cooperative")
    env = _env(script)
    with TerminalSession(
        [sys.executable, "-m", "imp", "new", "live resume", "--title", "live resume"],
        cwd=str(tmp_path), env=env, cols=80, rows=24,
    ) as s:
        quest_dir = _await_quest_dir(tmp_path, timeout=E2E_POLL_TIMEOUT)
        assert _poll(lambda: (quest_dir / "busy").exists(), timeout=E2E_POLL_TIMEOUT), _screen(s)
        s.send_signal(signal.SIGTERM)
        s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)
    assert _poll(lambda: _flock_free(quest_dir / "lock"), timeout=E2E_POLL_TIMEOUT)

    with TerminalSession(
        [sys.executable, "-m", "imp", "resume", quest_dir.name],
        cwd=str(tmp_path), env=env, cols=80, rows=24,
    ) as r:
        _at_empty_prompt(r)
        rows = r.lines()
        at = next(i for i, row in enumerate(rows) if _PLACEHOLDER in row)
        assert rows[at - 1] == "", _screen(r)
        assert rows[at - 2] != "", _screen(r)
        r.send("\x04")
        rc = r.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

        assert rc == 130, _screen(r)
        _assert_ctrl_d_block(r, quest_dir.name, "live resume")


@pytest.mark.parametrize("hint_first", [False, True], ids=["plain", "after-ctrl-c-hint"])
def test_message_after_clear_echoes_once(tmp_path: Path, hint_first: bool) -> None:
    script = _seed_scripted_gate_project(tmp_path, turns=_CHAT_TURNS)
    with TerminalSession(
        [sys.executable, "-m", "imp", "chat", "hello there"],
        cwd=str(tmp_path), env=_env(script), cols=80, rows=40,
    ) as s:
        first = _at_the_question(s, tmp_path)
        _at_empty_prompt(s)
        _send_and_see(s, "/clear\r", "ask anything, or /help")
        if hint_first:
            _send_and_see(s, "\x03", _HINT)
        _send_and_see(s, "next thing\r", "> next thing")

        def _second_quest_at_its_question() -> bool:
            journals = (project_state_dir(tmp_path) / "quests").glob("*/events.jsonl")
            return any(j.parent != first and "awaiting_user_input" in _event_types(j)
                       for j in journals)

        assert _poll(_second_quest_at_its_question, timeout=E2E_POLL_TIMEOUT), _screen(s)
        _at_empty_prompt(s)
        s.send("\x04")
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

        assert rc == 130, _screen(s)
        rows = s.lines()
        assert rows.count("> next thing") == 1, _screen(s)
        head = next((row for row in rows if row.endswith(" · next thing · archived")), None)
        assert head is not None, _screen(s)
        _assert_closing_layout(rows, head)


def _assert_one_blank_under_the_header(rows: list[str]) -> None:
    screen = "\n".join(rows)
    at = next((i for i, row in enumerate(rows) if row.endswith(" · t · new")), None)
    assert at is not None, screen
    assert rows[at + 1] == "", screen
    assert rows[at + 2].startswith("── work phase "), screen


def test_run_with_no_intent_has_one_blank_under_the_header_on_a_terminal(tmp_path: Path) -> None:
    script = _seed_scripted_gate_project(tmp_path, turns=_QUESTION_TURNS)
    with TerminalSession(
        [sys.executable, "-m", "imp", "new", "--title", "t", "--headless"],
        cwd=str(tmp_path), env=_env(script), cols=80, rows=24,
    ) as s:
        s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)
        _assert_one_blank_under_the_header(s.lines())


def test_run_with_no_intent_has_one_blank_under_the_header_when_piped(tmp_path: Path) -> None:
    script = _seed_scripted_gate_project(tmp_path, turns=_QUESTION_TURNS)
    out_path = tmp_path / "stdout.txt"
    with out_path.open("w") as out:
        subprocess.run(
            [sys.executable, "-m", "imp", "new", "--title", "t", "--headless"],
            cwd=tmp_path, env=_env(script), stdin=subprocess.DEVNULL, stdout=out,
            stderr=subprocess.STDOUT, timeout=E2E_EXIT_POLL_TIMEOUT)
    _assert_one_blank_under_the_header(out_path.read_text().splitlines())
