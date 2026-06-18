"""Deterministic relay-hibernation gate invariant (u8rfr8).

Drives a real `imp new` over a pty against the offline scripted provider to a
natural wait_for_user gate, then:

  reach gate (awaiting_user_input + daemon holds flock)
  -> SIGQUIT detach (daemon pid gone, flock released, state paused)
  -> imp resume (workflow_resumed + a FRESH daemon re-holds the flock).

Unconditional in the default suite: no IMP_LIVE_PROVIDERS / opted_in skip. The
pty is required — a non-tty would exercise the Part A headless path, not the tty
gate-render path this invariant guards.

The script calls `bash sleep 1` before `wait_for_user` so the CLI has time to
attach to the daemon socket (making attached_count > 0) before _hibernation_gate
checks it.  Without that delay the scripted provider reaches the gate before the
daemon's socket-serve task has accepted the incoming connection, causing an
immediate headless-pause exit rather than the attended block the test needs.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from imp.engine.change import Change
from impty import TerminalSession
from tests._live import _await_worktree
from tests.impty.e2e_support import can_pty, clean_env
from tests.test_relay_hibernation_live import _event_types, _flock_free, _poll

_WORKTREE_SRC = str(Path(__file__).parent.parent.parent / "src")

pytestmark = pytest.mark.skipif(not can_pty(), reason="pty not available")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args],
                   check=True, capture_output=True, text=True)


def _seed_scripted_gate_project(root: Path) -> Path:
    """Seed a scripted-provider repo whose single workflow step is an agent that
    calls bash (to let the CLI attach) then wait_for_user. Returns script.json."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n")
    (root / "README.md").write_text("seed\n")
    agents = root / "imp" / "workflows" / "imp" / "agents"
    agents.mkdir(parents=True)
    (agents / "gate.md").write_text(
        "---\nname: gate\ndescription: calls wait_for_user.\n"
        "tools: [bash, wait_for_user, complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\nAsk the human something, then wait.\n")
    (root / "imp" / "workflows" / "imp" / "workflow.yaml").write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n  - name: work\n    steps:\n"
        "      - name: gate\n        file: agents/gate.md\n        tier: medium\n"
        "        fs_permissions:\n"
        "          - {operations: [read],  paths: [\"**\"], mode: allow}\n"
        "          - {operations: [write], paths: [\"**\"], mode: allow}\n")
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(
        "providers: [scripted]\n"
        "tiers:\n"
        "  low: {scripted: x}\n"
        "  medium: {scripted: x}\n"
        "  high: {scripted: x}\n"
        "sandbox:\n  enabled: false\n")
    script_path = root / "script.json"
    script_path.write_text(json.dumps(
        {"default": [
            {"text": "waiting for you",
             "tools": [
                 # sleep 1 gives the CLI time to attach to the daemon socket so
                 # _hibernation_gate sees attached_count > 0 when wait_for_user runs.
                 {"name": "bash", "input": {"command": "sleep 1"}},
                 {"name": "wait_for_user", "input": {}},
             ]},
        ]}))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup")
    return script_path


def _wait_for_event(events_path: Path, event_type: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if events_path.exists() and event_type in _event_types(events_path):
            return True
        time.sleep(0.2)
    return False


def test_relay_hibernate_gate_then_resume_reengages(tmp_path: Path) -> None:
    script_path = _seed_scripted_gate_project(tmp_path)
    env = clean_env(
        IMP_SCRIPTED_SCRIPT=str(script_path),
        IMP_THEME="ansi",
        PYTHONPATH=_WORKTREE_SRC
        + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
    )

    s = TerminalSession(
        [sys.executable, "-m", "imp", "new", "relay-hibernate-gate"],
        cwd=str(tmp_path), env=env, cols=80, rows=24,
    )
    try:
        wt = _await_worktree(tmp_path, timeout=30.0)
        slug = wt.name
        events = wt / ".imp" / "change" / "events.jsonl"
        state_path = wt / ".imp" / "change" / "state" / "state.json"
        change = Change.load(tmp_path, slug)
        lock_path = change.change_dir / "lock"

        # The run reaches the natural wait_for_user gate while attended: the
        # daemon emits awaiting_user_input and holds the change flock there.
        # (The bash sleep 1 ensures the CLI attached before wait_for_user ran.)
        assert _wait_for_event(events, "awaiting_user_input", timeout=30.0), (
            "never reached the wait_for_user gate"
        )
        assert _poll(lambda: not _flock_free(lock_path), timeout=10.0), (
            "daemon never held the change flock at the gate"
        )
        daemon_pid = int(lock_path.read_text().strip())  # holder PID, before detach

        # SIGQUIT detaches the terminal client -> attached_count hits 0 ->
        # the daemon hibernates: pid exits, flock released, state paused.
        s.send_signal(signal.SIGQUIT)

        def _pid_gone() -> bool:
            try:
                os.kill(daemon_pid, 0)
                return False
            except ProcessLookupError:
                return True
            except PermissionError:
                return False

        assert _poll(_pid_gone, timeout=30.0), "daemon pid did not exit on detach"
        assert _poll(lambda: _flock_free(lock_path), timeout=30.0), "flock not released"
        assert json.loads(state_path.read_text())["status"] == "paused"
    finally:
        s.close()

    # imp resume -> a FRESH daemon re-acquires the flock and appends
    # workflow_resumed; its pid differs from the original holder.
    r = TerminalSession(
        [sys.executable, "-m", "imp", "resume", slug],
        cwd=str(tmp_path), env=env, cols=80, rows=24,
    )
    try:
        assert _poll(
            lambda: "workflow_resumed" in _event_types(events), timeout=60.0
        ), "resume never appended workflow_resumed"
        assert _poll(
            lambda: not _flock_free(lock_path), timeout=60.0
        ), "fresh daemon never re-acquired the flock"
        fresh_pid = int(lock_path.read_text().strip())
        assert fresh_pid != daemon_pid   # a NEW daemon process owns the resume
    finally:
        r.close()
