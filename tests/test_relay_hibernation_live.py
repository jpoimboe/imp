"""Relay-hibernation invariant, codified (u8rfr8).

IMP_LIVE_PROVIDERS=claude_code-gated. Drives, over a real pty via impty's
TerminalSession Python API:

  start → poll events.jsonl for the bash tool_call → SIGINT (the cooperative
  interrupt-steer gate renders on the pty; the run stays live, not yet paused)
  → SIGQUIT (daemon pid gone, flock released, state paused)
  → imp resume (workflow_resumed + a fresh daemon holds the flock).

The pty is required: a non-tty would exercise the Part A headless path, not the
tty gate-render path this test guards.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from imp.engine.state import LockHeld, ProjectLock
from impty import TerminalSession
from tests._live import _await_change_dir, opted_in
from tests.conftest import _init_repo

_WORKTREE_SRC = str(Path(__file__).parent.parent / "src")
_CC_STARTUP_GRACE = 2.0  # seconds to allow the CC subprocess to start


def _seed_cc_project(root: Path) -> None:
    _init_repo(root)
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(textwrap.dedent("""\
        ---
        name: writer
        description: A test agent that calls bash then complete_step.
        tools: [complete_step, bash]
        output:
          status: {type: enum, values: [Success, Error]}
        ---
        Run this shell command via bash: `sleep 30`. After bash returns
        (even if it was interrupted), call complete_step with status=Success.
        Do not call any other tool.
    """))
    (wf / "workflow.yaml").write_text(textwrap.dedent("""\
        name: t
        description: t
        defaults: {max_retries: 2, max_loops: 6, on_max_retries: {pause: true}}
        phases:
          - name: only
            steps:
              - {name: w, file: agents/writer.md, tier: low}
    """))
    (root / "imp" / "project.yaml").write_text(textwrap.dedent("""\
        default-workflow: imp
        default-base: main
        providers: [claude_code]
        tiers:
          low: {claude_code: claude-haiku-4-5-20251001}
          medium: {claude_code: claude-haiku-4-5-20251001}
          high: {claude_code: claude-haiku-4-5-20251001}
    """))
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "imp setup"],
        cwd=root, check=True,
    )


def _wait_for_bash_tool_call(events_path: Path, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if events_path.exists():
            try:
                text = events_path.read_text()
            except OSError:
                text = ""
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (rec.get("type") == "tool_call"
                        and rec.get("payload", {}).get("name") == "bash"):
                    return True
        time.sleep(0.2)
    return False


def _flock_free(lock_path: Path) -> bool:
    try:
        with ProjectLock(lock_path).acquire(timeout=0):
            return True
    except LockHeld:
        return False


def _poll(predicate, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def _event_types(events_path: Path) -> list[str]:
    out: list[str] = []
    for line in events_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line)["type"])
        except (json.JSONDecodeError, KeyError):
            continue
    return out


@pytest.mark.skipif(
    not opted_in("claude_code"),
    reason="claude_code not in IMP_LIVE_PROVIDERS",
)
def test_relay_hibernate_then_resume_reengages(tmp_path: Path) -> None:
    assert shutil.which("claude") is not None, (
        "claude_code opted in via IMP_LIVE_PROVIDERS but `claude` CLI not on PATH"
    )
    _seed_cc_project(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    _pypath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _WORKTREE_SRC + (os.pathsep + _pypath if _pypath else "")
    env.setdefault("IMP_THEME", "ansi")

    s = TerminalSession(
        [sys.executable, "-m", "imp", "new", "relay-hibernate"],
        cwd=str(tmp_path), env=env, cols=80, rows=24,
    )
    try:
        # imp new starts an un-materialized draft: no worktree, state+events live
        # at the permanent control-root home .imp/changes/<id>/. The minimal
        # workflow never calls create_and_enter_worktree, so it runs as a draft.
        change_dir = _await_change_dir(tmp_path, timeout=30.0)
        slug = change_dir.name
        events = change_dir / "events.jsonl"
        state_path = change_dir / "state" / "state.json"
        lock_path = change_dir / "lock"

        assert _wait_for_bash_tool_call(events, timeout=120.0), "never saw bash tool_call"
        time.sleep(_CC_STARTUP_GRACE)

        # SIGINT mid-turn is the cooperative interrupt: the run yields inline to
        # the human (it does NOT pause yet — that happens on the SIGQUIT detach
        # below). The steer gate renders INTERRUPT_STEER_HINT on the pty, so the
        # human can redirect or detach. Anchor on that hint, not "paused".
        s.send_signal(signal.SIGINT)
        # The hint renders within a few seconds idle; the timeout is generous
        # because under the full -n 4 live gate the render/event loop can be
        # starved. It still requires the hint to appear — only tolerates lag.
        assert _poll(
            lambda: "type to redirect" in "\n".join(s.lines()).lower(), timeout=60.0
        ), "interrupt-steer gate never rendered"
        daemon_pid = int(lock_path.read_text().strip())  # holder PID, read before detach

        # SIGQUIT → deliberate detach: daemon hibernates, flock released, paused.
        s.send_signal(signal.SIGQUIT)

        def _pid_gone() -> bool:
            try:
                os.kill(daemon_pid, 0)
                return False
            except ProcessLookupError:
                return True
            except PermissionError:
                return False

        assert _poll(_pid_gone, timeout=60.0), "daemon pid did not exit on detach"
        assert _poll(lambda: _flock_free(lock_path), timeout=60.0), "flock not released"
        assert json.loads(state_path.read_text())["status"] == "paused"
    finally:
        s.close()

    # imp resume → a fresh daemon re-acquires the flock, appends workflow_resumed.
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
