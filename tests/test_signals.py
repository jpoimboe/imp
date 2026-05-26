"""Pause-on-interrupt: KeyboardInterrupt + EOFError save state and exit clean."""

from __future__ import annotations

from pathlib import Path

from imp.cli.signals import pause_and_exit
from imp.engine.change import Change
from imp.engine.state import State


def _seed_change(tmp_path: Path) -> Change:
    (tmp_path / ".imp" / "workflows" / "default").mkdir(parents=True)
    (tmp_path / ".imp" / "workflows" / "default" / "workflow.yaml").write_text(
        "name: x\ndescription: y\nphases: [p]\n"
        "defaults:\n  max_retries: 0\n  on_max_retries: {kind: pause}\n"
        "p:\n  steps: []\n"
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: [{provider: anthropic, model: x}]}\n"
    )
    return Change.create(tmp_path, title="t")


def test_pause_writes_state_and_event(tmp_path: Path) -> None:
    change = _seed_change(tmp_path)
    State(
        change_id=change.id, phase="p", step="s",
        step_attempt=1, phase_attempt=0, status="running",
    ).save(change.state_dir)

    pause_and_exit(change, reason="ctrl-c")

    state = State.load(change.state_dir)
    assert state.status == "paused"
    events = list(change.event_log().read())
    assert any(e["type"] == "workflow_paused" for e in events)
    assert any(
        e["type"] == "workflow_paused" and e["payload"].get("reason") == "ctrl-c"
        for e in events
    )


def test_pause_is_idempotent(tmp_path: Path) -> None:
    """Calling twice doesn't crash. State is overwritten (last wins);
    events accumulate (two workflow_paused entries in the log)."""
    change = _seed_change(tmp_path)
    State(
        change_id=change.id, phase="p", step=None,
        step_attempt=0, phase_attempt=0, status="running",
    ).save(change.state_dir)
    pause_and_exit(change, reason="x")
    pause_and_exit(change, reason="y")
    state = State.load(change.state_dir)
    assert state.status == "paused"
    # Events accumulate — confirm two workflow_paused entries with the
    # correct reasons in order.
    events = list(change.event_log().read())
    paused_events = [e for e in events if e["type"] == "workflow_paused"]
    assert len(paused_events) == 2
    assert paused_events[0]["payload"]["reason"] == "x"
    assert paused_events[1]["payload"]["reason"] == "y"


def test_pause_when_no_state_file(tmp_path: Path) -> None:
    """If state.json doesn't exist yet, pause_and_exit still appends the
    workflow_paused event (defensive: always record the pause)."""
    change = _seed_change(tmp_path)
    # Delete state.json to simulate pre-state-save interrupt
    (change.state_dir / "state.json").unlink()

    pause_and_exit(change, reason="early")

    events = list(change.event_log().read())
    assert any(
        e["type"] == "workflow_paused" and e["payload"].get("reason") == "early"
        for e in events
    )


import os
import signal
import subprocess
import sys
import time
import contextlib

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
def test_install_process_group_makes_us_leader() -> None:
    """After install_process_group, our PID == our PGID."""
    from imp.cli.signals import install_process_group

    install_process_group()
    assert os.getpid() == os.getpgid(0)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
@pytest.mark.skipif(sys.platform == "darwin", reason="POSIX /proc-walk not available on macOS")
def test_tool_kill_escalation_terminates_child(tmp_path: Path) -> None:
    """Spawn a child in our PG, call escalation, child must be dead."""
    from imp.cli.signals import install_process_group, tool_kill_escalation

    install_process_group()
    # Child sleeps; we expect it to be killed
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    # Brief settle so /proc/<child_pid>/stat is available
    time.sleep(0.1)
    try:
        # Send SIGTERM-then-SIGKILL to the PG (which includes us — pytest
        # would die — so we exclude self by passing kill_self=False)
        tool_kill_escalation(timeout=0.5, kill_self=False)
        # Child should be gone in well under our 5s test budget
        for _ in range(50):
            if child.poll() is not None:
                break
            time.sleep(0.1)
        assert child.poll() is not None, "child wasn't killed"
    finally:
        with contextlib.suppress(ProcessLookupError):
            child.kill()
