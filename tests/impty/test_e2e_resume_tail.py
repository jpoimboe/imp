"""`imp resume` replays the agent's last message and what followed, then runs on."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest

from imp.engine.quest import Quest
from impty import TerminalSession
from tests.impty.e2e_support import (
    E2E_EXIT_POLL_TIMEOUT,
    E2E_POLL_TIMEOUT,
    E2E_RENDER_POLL_TIMEOUT,
    _flock_free,
    _poll,
    can_pty,
)
from tests.impty.test_e2e_leave_exit_codes import _at_the_question, _await_quest_dir, _env
from tests.impty.test_e2e_relay_hibernation_gate import (
    _ATTACH_POLL,
    _seed_scripted_gate_project,
)

pytestmark = pytest.mark.skipif(not can_pty(), reason="pty not available")


def _bash(command: str) -> dict[str, Any]:
    return {"name": "bash", "input": {"command": command}}


# The resumed run replays the script from turn 0: this guard parks it there, before
# any new prose. Guard first so the resumed running-tool footer reads it.
_GUARD = _bash(f'[ -f "$IMP_QUEST_DIR/left" ] && sleep 300; {_ATTACH_POLL}')
_LEAVE = _bash(': > "$IMP_QUEST_DIR/left"; sleep 120')
_QUESTION: dict[str, Any] = {"text": "Which file should I edit?"}
_DONE: dict[str, Any] = {"text": "done"}
_PARKED = re.compile(r'^\S .+ · 1 call · bash \[ -f "\$IMP_QUEST_DIR/left" \]')


def _leave_mid_run(s: TerminalSession, quest_dir: Path) -> None:
    assert _poll(lambda: (quest_dir / "left").exists(), timeout=E2E_POLL_TIMEOUT), (
        "never reached the long check\n" + "\n".join(s.lines()))
    s.send("\x04")
    assert s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT) == 130, "\n".join(s.lines())


def _resumed_screen(root: Path, env: dict[str, str], quest_dir: Path) -> list[str]:
    """Resume, wait until the guard parks the agent, leave again; return the parked screen."""
    assert _poll(lambda: _flock_free(quest_dir / "lock"), timeout=E2E_POLL_TIMEOUT)
    with TerminalSession([sys.executable, "-m", "imp", "resume", quest_dir.name],
                         cwd=str(root), env=env, cols=80, rows=24) as r:
        assert _poll(lambda: any(_PARKED.match(ln) for ln in r.lines()),
                     timeout=E2E_RENDER_POLL_TIMEOUT), "\n".join(r.lines())
        rows = r.lines()
        r.send("\x04")
        assert r.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT) == 130, "\n".join(r.lines())
    return rows


def _assert_tail(rows: list[str], header: str, tail: list[str | re.Pattern[str]],
                 absent: tuple[str, ...]) -> None:
    """`tail` sits under the header's banner after one blank, each row spaced by one
    blank, then the resumed step header and the parked footer."""
    screen = "\n".join(rows)
    assert rows.count(header) == 1, screen
    at = rows.index(header)
    start = next((i for i, ln in enumerate(rows) if ln == tail[0]), None)
    assert start is not None and start > at + 2, screen
    assert rows[start - 1] == "" and rows[start - 2] != "", screen
    expected = [*tail, "gate (resume) · x", _PARKED]
    got = rows[start:start + 2 * len(expected) - 1]
    assert len(got) == 2 * len(expected) - 1, screen
    for i, want in enumerate(expected):
        row = got[2 * i]
        assert (want.match(row) if isinstance(want, re.Pattern) else row == want), screen
        if i:
            assert got[2 * i - 1] == "", screen
    assert len(rows) > start + len(got) and set(rows[start + len(got)]) == {"─"}, screen
    for text in absent:
        assert not any(text in ln for ln in rows), screen


def test_resuming_a_draft_replays_the_last_message_and_its_call(tmp_path: Path) -> None:
    turns: list[dict[str, Any]] = [
        {"tools": [_GUARD]}, _QUESTION,
        {"text": "Running the long check.", "tools": [_LEAVE]}, _DONE,
    ]
    env = _env(_seed_scripted_gate_project(tmp_path, turns=turns))
    with TerminalSession([sys.executable, "-m", "imp", "new", "opening intent", "--title", "t"],
                         cwd=str(tmp_path), env=env, cols=80, rows=24) as s:
        quest_dir = _at_the_question(s, tmp_path)
        s.send("the parser\r")
        _leave_mid_run(s, quest_dir)
    assert not Quest.load(tmp_path, quest_dir.name).materialized

    _assert_tail(_resumed_screen(tmp_path, env, quest_dir), f"{quest_dir.name} · t · resuming",
                 ["• Running the long check.", "  1 call · bash"],
                 absent=("opening intent", "Which file should I edit?", "the parser"))


def test_resuming_after_a_silent_call_replays_from_the_question(tmp_path: Path) -> None:
    turns: list[dict[str, Any]] = [
        {"tools": [_GUARD]}, _QUESTION, {"tools": [_LEAVE]}, _DONE,
    ]
    env = _env(_seed_scripted_gate_project(tmp_path, turns=turns))
    with TerminalSession([sys.executable, "-m", "imp", "new", "opening intent", "--title", "t"],
                         cwd=str(tmp_path), env=env, cols=80, rows=24) as s:
        quest_dir = _at_the_question(s, tmp_path)
        s.send("the parser\r")
        _leave_mid_run(s, quest_dir)
    assert not Quest.load(tmp_path, quest_dir.name).materialized

    _assert_tail(_resumed_screen(tmp_path, env, quest_dir), f"{quest_dir.name} · t · resuming",
                 ["• Which file should I edit?", "> the parser", "  1 call · bash"],
                 absent=("opening intent",))


def test_resuming_a_built_quest_replays_the_tail_above_the_resumed_step(
        tmp_path: Path) -> None:
    turns: list[dict[str, Any]] = [
        {"tools": [_GUARD]},
        {"text": "Running the long check.", "tools": [
            {"name": "create_and_enter_worktree", "input": {}}, _LEAVE]},
        _DONE,
    ]
    # hard_kill: the default mode's killed call would let the script reach `done` first.
    env = _env(_seed_scripted_gate_project(tmp_path, turns=turns, interrupt="hard_kill"))
    with TerminalSession([sys.executable, "-m", "imp", "new", "opening intent", "--title", "t"],
                         cwd=str(tmp_path), env=env, cols=80, rows=24) as s:
        quest_dir = _await_quest_dir(tmp_path, timeout=E2E_POLL_TIMEOUT)
        _leave_mid_run(s, quest_dir)
    assert Quest.load(tmp_path, quest_dir.name).materialized

    _assert_tail(_resumed_screen(tmp_path, env, quest_dir), f"{quest_dir.name} · t · resuming",
                 ["• Running the long check.", re.compile(r"^  1 call · bash( · \d+s)?$"),
                  re.compile(r"^paused · resumed .+ later$")],
                 absent=("opening intent", "• done"))
