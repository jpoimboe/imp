"""A user-ended leave exits 130 over a real terminal, with its own block."""

from __future__ import annotations

import json
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
    _flock_free,
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


def _events(quest_dir: Path) -> list[dict]:
    return [json.loads(ln) for ln in (quest_dir / "events.jsonl").read_text().splitlines()]


def _assert_closing_layout(rows: list[str], head: str) -> int:
    """`head` sits under exactly one blank row; returns its index."""
    screen = "\n".join(rows)
    assert rows.count(head) == 1, screen
    at = rows.index(head)
    assert at >= 2, screen
    assert rows[at - 1] == "", screen
    assert rows[at - 2] != "", screen
    return at


def _assert_pause_block(s: TerminalSession, head: str, quest_id: str, title: str) -> int:
    """The closing layout above `head`, then the identity and resume rows; returns its index."""
    rows = s.lines()
    at = _assert_closing_layout(rows, head)
    assert rows[at + 1:at + 3] == [
        f"{quest_id} · {title}", f"resume: imp resume {quest_id}"], "\n".join(rows)
    return at


def _assert_ctrl_d_block(s: TerminalSession, quest_id: str, title: str) -> int:
    assert "> Ctrl-D" not in s.lines(), "\n".join(s.lines())
    return _assert_pause_block(s, "paused · Ctrl-D", quest_id, title)


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
        _assert_ctrl_d_block(s, quest_dir.name, "ctrl d gate")


def test_pause_at_a_built_quests_question_pauses_like_ctrl_d(tmp_path: Path) -> None:
    script = _seed_scripted_gate_project(tmp_path, turns=[
        {"text": "setting up", "tools": [
            {"name": "create_and_enter_worktree", "input": {}},
            {"name": "bash", "input": {"command": _ATTACH_POLL}},
        ]},
        {"text": "waiting for you"},
    ])
    env = _env(script)
    with TerminalSession(
        [sys.executable, "-m", "imp", "new", "pause-gate", "--title", "pause gate"],
        cwd=str(tmp_path), env=env, cols=80, rows=24,
    ) as s:
        quest_dir = _at_the_question(s, tmp_path)
        assert Quest.load(tmp_path, quest_dir.name).materialized   # a built quest
        s.send("/pause\r")
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

        assert rc == 130, "\n".join(s.lines())
        at = _assert_pause_block(s, "paused · /pause", quest_dir.name, "pause gate")
        assert s.lines()[at - 2] == "> /pause", "\n".join(s.lines())

    paused = [e["payload"] for e in _events(quest_dir) if e["type"] == "workflow_paused"]
    assert paused[-1]["interrupt_reason"] == "pause"
    state = json.loads((quest_dir / "state" / "state.json").read_text())
    assert state["status"] == "paused"
    # A question landing: resume re-asks it.
    assert (state["resume_target"], state["resume_pending"],
            state["resume_should_continue"]) == (None, "gate", False)

    with TerminalSession(
        [sys.executable, "-m", "imp", "resume", quest_dir.name],
        cwd=str(tmp_path), env=env, cols=80, rows=24,
    ) as r:
        assert _poll(lambda: any("waiting for you" in ln for ln in r.lines()),
                     timeout=E2E_POLL_TIMEOUT), "\n".join(r.lines())
        r.wait_idle()
        r.send("go on\r")
        # The reply lands as Ctrl-D's would; a prompt-interrupted target swallows it.
        assert _poll(lambda: any(
            e["type"] == "user_message" and "go on" in json.dumps(e["payload"])
            for e in _events(quest_dir)), timeout=E2E_POLL_TIMEOUT), "\n".join(r.lines())
        assert not any("interrupted ·" in ln for ln in r.lines())


@pytest.mark.parametrize(("command", "tail", "resume_suffix"), [
    ("/pause", "paused", ""),
    ("/archive", "archived", " — or find it later with imp search"),
])
def test_leave_command_in_chat_after_an_exchange_exits_130(
    tmp_path: Path, command: str, tail: str, resume_suffix: str,
) -> None:
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
        assert not Quest.load(tmp_path, quest_dir.name).materialized
        s.send(f"{command}\r")
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

        assert rc == 130, "\n".join(s.lines())
        assert_screen(s, contains=(
            f" · {tail}", f"resume: imp resume {quest_dir.name}{resume_suffix}"))


@pytest.mark.parametrize("interrupt", ["cooperative", "hard_kill"])
def test_sigterm_mid_run_on_a_built_quest_exits_130(tmp_path: Path, interrupt: str) -> None:
    script = _seed_scripted_gate_project(tmp_path, interrupt=interrupt, turns=[
        {"text": "working", "tools": [
            {"name": "create_and_enter_worktree", "input": {}},
            # Attached, then busy once: the marker proves the step is mid-run.
            {"name": "bash", "input": {
                "command": f'{_ATTACH_POLL}; [ -f "$IMP_QUEST_DIR/busy" ] && exit 0; '
                           ': > "$IMP_QUEST_DIR/busy"; sleep 30'}},
        ]},
        {"text": "the next turn ran"},
    ])
    env = _env(script)
    with TerminalSession(
        [sys.executable, "-m", "imp", "new", "sigterm-mid-run",
         "--title", "sigterm mid run"],
        cwd=str(tmp_path), env=env, cols=80, rows=24,
    ) as s:
        quest_dir = _await_quest_dir(tmp_path, timeout=E2E_POLL_TIMEOUT)
        assert _poll(lambda: (quest_dir / "busy").exists(), timeout=E2E_POLL_TIMEOUT), (
            "the step never went busy\n" + "\n".join(s.lines()))
        assert Quest.load(tmp_path, quest_dir.name).materialized
        s.send_signal(signal.SIGTERM)
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

        assert rc == 130, "\n".join(s.lines())
        assert_screen(s, contains=("paused · SIGTERM",))

    # Left mid-run: resume runs the agent on, not the interrupted-steer prompt.
    assert _poll(lambda: _flock_free(quest_dir / "lock"), timeout=E2E_POLL_TIMEOUT)

    def _after_resume() -> list[dict]:
        events = _events(quest_dir)
        last = max((i for i, e in enumerate(events) if e["type"] == "workflow_resumed"),
                   default=None)
        return [] if last is None else events[last + 1:]

    with TerminalSession(
        [sys.executable, "-m", "imp", "resume", quest_dir.name],
        cwd=str(tmp_path), env=env, cols=80, rows=24,
    ) as r:
        assert _poll(lambda: any(
            e["type"] == "awaiting_user_input"
            and e["payload"].get("last_agent_message") == "the next turn ran"
            for e in _after_resume()), timeout=E2E_POLL_TIMEOUT), "\n".join(r.lines())
        assert not any(e["type"] == "awaiting_user_input" and e["payload"].get("interrupt")
                       for e in _after_resume())
        assert not any("interrupted ·" in ln for ln in r.lines())


def test_ctrl_d_mid_run_on_a_built_quest_exits_130(tmp_path: Path) -> None:
    script = _seed_scripted_gate_project(tmp_path, turns=[
        {"text": "working", "tools": [
            {"name": "create_and_enter_worktree", "input": {}},
            # Attached, then busy once: the marker proves the step is mid-run.
            {"name": "bash", "input": {
                "command": f'{_ATTACH_POLL}; [ -f "$IMP_QUEST_DIR/busy" ] && exit 0; '
                           ': > "$IMP_QUEST_DIR/busy"; sleep 30'}},
        ]},
        {"text": "the next turn ran"},
    ])
    with TerminalSession(
        [sys.executable, "-m", "imp", "new", "ctrl-d-mid-run", "--title", "ctrl d mid run"],
        cwd=str(tmp_path), env=_env(script), cols=80, rows=24,
    ) as s:
        quest_dir = _await_quest_dir(tmp_path, timeout=E2E_POLL_TIMEOUT)
        assert _poll(lambda: (quest_dir / "busy").exists(), timeout=E2E_POLL_TIMEOUT), (
            "the step never went busy\n" + "\n".join(s.lines()))
        assert Quest.load(tmp_path, quest_dir.name).materialized
        s.send("\x04")
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

        assert rc == 130, "\n".join(s.lines())
        _assert_ctrl_d_block(s, quest_dir.name, "ctrl d mid run")


def test_ctrl_d_in_chat_after_an_exchange_exits_130_archived(tmp_path: Path) -> None:
    # No create_and_enter_worktree: the chat stays phase-0, so Ctrl-D archives it.
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
        _assert_closing_layout(s.lines(), f"{quest_dir.name} · hello there · archived")
