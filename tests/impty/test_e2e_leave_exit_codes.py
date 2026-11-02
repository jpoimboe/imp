"""A user-ended leave exits 130 over a real terminal, with its own block."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from imp.engine.quest import Quest
from imp.paths import project_state_dir
from impty import TerminalSession
from tests.impty.e2e_support import (
    E2E_EXIT_POLL_TIMEOUT,
    E2E_POLL_TIMEOUT,
    _event_types,
    _poll,
    can_pty,
    clean_env,
)
from tests.impty.matchers import assert_screen
from tests.impty.test_e2e_relay_hibernation_gate import (
    _ATTACH_POLL,
    _seed_scripted_gate_project,
    _wait_for_event,
)

pytestmark = pytest.mark.skipif(not can_pty(), reason="pty not available")

_WORKTREE_SRC = str(Path(__file__).parent.parent.parent / "src")


def _env(script_path: Path) -> dict[str, str]:
    return clean_env(
        IMP_SCRIPTED_SCRIPT=str(script_path),
        IMP_THEME="ansi",
        PYTHONPATH=_WORKTREE_SRC
        + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
    )


def _await_quest_dir(control_root: Path, timeout: float) -> Path:
    quests = project_state_dir(control_root) / "quests"
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = sorted(quests.glob("*/quest.json")) if quests.is_dir() else []
        if found:
            return found[0].parent
        time.sleep(0.1)
    raise TimeoutError(f"no quest home appeared under {quests} within {timeout}s")


def _at_the_question(s: TerminalSession, control_root: Path) -> Path:
    """Block until the turn-end question is journaled and the screen is quiet;
    return the quest home.
    """
    quest_dir = _await_quest_dir(control_root, timeout=E2E_POLL_TIMEOUT)
    events = quest_dir / "events.jsonl"
    assert _wait_for_event(events, "awaiting_user_input", timeout=E2E_POLL_TIMEOUT), (
        "never reached the turn-end question\n" + "\n".join(s.lines()))
    s.wait_idle()
    return quest_dir


def test_ctrl_d_at_a_built_quests_question_exits_130(tmp_path: Path) -> None:
    script = _seed_scripted_gate_project(tmp_path, turns=[
        {"text": "setting up", "tools": [
            {"name": "create_and_enter_worktree", "input": {}},
            {"name": "bash", "input": {"command": _ATTACH_POLL}},
        ]},
        {"text": "waiting for you"},
    ])
    with TerminalSession(
        [sys.executable, "-m", "imp", "new", "ctrl-d-gate", "--title", "ctrl d gate"],
        cwd=str(tmp_path), env=_env(script), cols=80, rows=24,
    ) as s:
        quest_dir = _at_the_question(s, tmp_path)
        assert Quest.load(tmp_path, quest_dir.name).materialized   # a built quest
        s.send("\x04")
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

        assert rc == 130, "\n".join(s.lines())
        assert_screen(s, contains=("paused · Ctrl-D",))


def test_sigterm_mid_run_on_a_built_quest_exits_130(tmp_path: Path) -> None:
    script = _seed_scripted_gate_project(tmp_path, turns=[
        {"text": "working", "tools": [
            {"name": "create_and_enter_worktree", "input": {}},
            # Attached, then busy: the marker proves the step is mid-run.
            {"name": "bash", "input": {
                "command": f'{_ATTACH_POLL}; : > "$IMP_QUEST_DIR/busy"; sleep 30'}},
        ]},
        {"text": "done"},
    ])
    with TerminalSession(
        [sys.executable, "-m", "imp", "new", "sigterm-mid-run",
         "--title", "sigterm mid run"],
        cwd=str(tmp_path), env=_env(script), cols=80, rows=24,
    ) as s:
        quest_dir = _await_quest_dir(tmp_path, timeout=E2E_POLL_TIMEOUT)
        assert _poll(lambda: (quest_dir / "busy").exists(), timeout=E2E_POLL_TIMEOUT), (
            "the step never went busy\n" + "\n".join(s.lines()))
        assert Quest.load(tmp_path, quest_dir.name).materialized
        s.send_signal(signal.SIGTERM)
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

        assert rc == 130, "\n".join(s.lines())
        assert_screen(s, contains=("paused · SIGTERM",))


def test_ctrl_d_in_chat_after_an_exchange_exits_130_shelved(tmp_path: Path) -> None:
    # No create_and_enter_worktree: the chat stays phase-0, so Ctrl-D shelves it.
    script = _seed_scripted_gate_project(tmp_path, turns=[
        {"text": "setting up", "tools": [
            {"name": "bash", "input": {"command": _ATTACH_POLL}},
        ]},
        {"text": "waiting for you"},
    ])
    with TerminalSession(
        [sys.executable, "-m", "imp", "chat", "hello there"],
        cwd=str(tmp_path), env=_env(script), cols=80, rows=24,
    ) as s:
        quest_dir = _at_the_question(s, tmp_path)
        # The seeded opening is journaled as a user_message: that is the exchange.
        assert "user_message" in _event_types(quest_dir / "events.jsonl")
        assert not Quest.load(tmp_path, quest_dir.name).materialized
        s.send("\x04")
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

        assert rc == 130, "\n".join(s.lines())
        assert_screen(s, contains=("shelved",))
