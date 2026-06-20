"""Deterministic relay-hibernation gate invariant (u8rfr8).

Drives a real `imp new` over a pty against the offline scripted provider to a
natural wait_for_user gate, then:

  reach gate (awaiting_user_input + daemon holds flock)
  -> SIGQUIT detach (daemon pid gone, flock released, state paused)
  -> imp resume (workflow_resumed + a FRESH daemon re-holds the flock).

Unconditional in the default suite: no IMP_LIVE_PROVIDERS / opted_in skip. The
pty is required — a non-tty would exercise the Part A headless path, not the tty
gate-render path this invariant guards.

The script polls the change's events.jsonl for the daemon-emitted `cli_attached`
event before `wait_for_user`, so the scripted provider blocks until the CLI has
actually attached to the daemon socket (attached_count > 0) before
_hibernation_gate checks it. The daemon writes `cli_attached` on the 0 -> 1
attach transition; gating on that real signal — instead of a fixed sleep that
races the attach — is what makes the test deterministic on loaded CI.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from imp.engine.change import Change
from impty import TerminalSession
from tests._live import _await_worktree
from tests.impty.e2e_support import _MINIMAL_CONFIG, _git, can_pty, clean_env
from tests.test_relay_hibernation_live import _event_types, _flock_free, _poll

_WORKTREE_SRC = str(Path(__file__).parent.parent.parent / "src")

# Bounded poll (~20s ceiling, then exit 0) on the change's events.jsonl for a
# cli_attached marker that is FRESH for the current daemon instance. The journal
# is at $IMP_CHANGE_DIR/events.jsonl (the permanent control-root home).
#
# cli_attached is written to the PERSISTENT journal and survives hibernation, so
# the first-run marker is still present on the resume leg. The SAME scripted
# script runs on BOTH legs, so a naive `grep cli_attached` matches the STALE
# first-run marker on resume and short-circuits before the resume CLI has
# attached; the gate then sees attached_count == 0 and the fresh daemon
# hibernates instead of holding the flock. To distinguish the current instance's
# attach, anchor the search to the last workflow_resumed: the daemon appends
# workflow_resumed synchronously at _bound entry, BEFORE the socket starts
# accepting (its start_soon tasks only run once _bound first awaits), so the
# resume-leg cli_attached is guaranteed to land AFTER it. On the first leg there
# is no workflow_resumed, base stays 0, and the whole file is scanned.
#
# Match the serialized JSON field `"type": "cli_attached"` / `"type":
# "workflow_resumed"` (unescaped quotes) and NOT the bare word: the executor
# journals THIS bash command's own tool_call event -- command text and all --
# BEFORE running it (dispatch.py's append_tool_call precedes the tool
# invocation), and the same command text is echoed in the preceding llm_response
# raw payload. In both, json.dumps backslash-escapes the embedded quotes
# (\"type\": \"cli_attached\"), so a pattern with literal unescaped quotes
# matches only the real daemon event records and never this command's self-echo.
# `: ?` tolerates either json separator spacing.
_ATTACH_POLL = (
    'J="${IMP_CHANGE_DIR}/events.jsonl"; '
    "for _ in $(seq 200); do "
    "base=$(grep -nE '\"type\": ?\"workflow_resumed\"' \"$J\" 2>/dev/null "
    "| tail -1 | cut -d: -f1); base=${base:-0}; "
    "tail -n +$((base + 1)) \"$J\" 2>/dev/null "
    "| grep -qE '\"type\": ?\"cli_attached\"' && break; "
    "sleep 0.1; done; true"
)

pytestmark = pytest.mark.skipif(not can_pty(), reason="pty not available")


def _seed_scripted_gate_project(root: Path) -> Path:
    """Seed a scripted-provider repo whose single workflow step is an agent that
    calls bash (polling for the cli_attached marker) then wait_for_user. Returns script.json."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n")
    (root / "README.md").write_text("seed\n")
    agents = root / "imp" / "workflows" / "imp" / "agents"
    agents.mkdir(parents=True)
    (agents / "gate.md").write_text(
        "---\nname: gate\ndescription: calls wait_for_user.\n"
        "tools: [bash, wait_for_user, complete_step, create_and_enter_worktree]\n"
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
    (root / ".imp" / "config.yaml").write_text(_MINIMAL_CONFIG)
    script_path = root / "script.json"
    script_path.write_text(json.dumps(
        {"default": [
            {"text": "waiting for you",
             "tools": [
                 {"name": "create_and_enter_worktree", "input": {}},
                 {"name": "bash", "input": {"command": _ATTACH_POLL}},
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
        change = Change.load(tmp_path, slug)
        events = change.change_dir / "events.jsonl"
        state_path = change.change_dir / "state" / "state.json"
        lock_path = change.change_dir / "lock"

        # The daemon journals cli_attached on the 0 -> 1 attach; the scripted
        # bash step blocks on that marker, so the provider only reaches
        # wait_for_user once attached_count > 0 (no fixed-sleep race).
        assert _wait_for_event(events, "cli_attached", timeout=30.0), (
            "daemon never journaled cli_attached after the CLI attached"
        )
        # The run reaches the natural wait_for_user gate while attended: the
        # daemon emits awaiting_user_input and holds the change flock there.
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
