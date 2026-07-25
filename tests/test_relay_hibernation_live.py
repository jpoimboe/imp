"""Relay-hibernation invariant, codified (u8rfr8).

Gated behind `--run-live` (claude_code). Drives, over a real pty via impty's
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
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from impty import TerminalSession
from tests._live import (
    _await_change_dir,
    evidence,
    sandbox_sleep_precondition_failed,
    wait_for_bash_tool_call,
    wait_for_status,
)
from tests.conftest import _init_repo
from tests.impty.e2e_support import (
    E2E_POLL_TIMEOUT,
    E2E_RENDER_POLL_TIMEOUT,
    _event_types,
    _flock_free,
    _poll,
)

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


def _interrupt_gate_engaged(events_path: Path) -> bool:
    """True once the durable cooperative-interrupt steer signal is persisted: an
    `awaiting_user_input` record whose payload `interrupt` flag is True. The
    engine journals this event (event_log.append, src/imp/engine/completion.py)
    the instant the steer gate engages — before any socket relay or pty repaint —
    so it is the render-independent authority that the gate engaged, unlike the pty screen
    which can lag. A bare-turn-end yield also emits awaiting_user_input but with
    interrupt=False, so the flag, not the type alone, is the discriminator."""
    if not events_path.exists():
        return False
    try:
        body = events_path.read_text()
    except OSError:
        return False
    for line in body.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (rec.get("type") == "awaiting_user_input"
                and rec.get("payload", {}).get("interrupt") is True):
            return True
    return False


def _usr1_stack_dump(daemon_pid: int, quest_dir: Path, timeout: float = 5.0) -> str:
    """Signal the daemon to dump its own stacks and return the freshly appended
    text. The daemon installs a SIGUSR1 stack dumper at quest_dir/stack-dump.txt
    (src/imp/remote/session/daemon.py); return the newly written section, or a
    marker string if the daemon is gone or wrote nothing within `timeout`."""
    dump = quest_dir / "stack-dump.txt"
    before = dump.read_text() if dump.exists() else ""
    try:
        os.kill(daemon_pid, signal.SIGUSR1)
    except (ProcessLookupError, PermissionError) as exc:
        return f"<could not signal daemon pid {daemon_pid}: {exc!r}>"
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = dump.read_text() if dump.exists() else ""
        if current != before:
            return current[len(before):] or current
        time.sleep(0.2)
    return f"<no new stack dump at {dump} within {timeout}s>"


def _relay_skip_or_fail(
    what: str,
    events: Path,
    screen: list[str],
    daemon_pid: int,
    quest_dir: Path,
) -> None:
    """On a gate miss: skip legibly when imp's bash-sandbox overlay mount failed
    (the writer's `sleep 30` never ran, so the mid-turn SIGINT never interrupted
    a live turn); otherwise fail with diagnostics — the current screen lines, the
    events.jsonl event-type sequence, and the daemon's own SIGUSR1 stack dump."""
    reason = sandbox_sleep_precondition_failed(events)
    if reason is not None:
        pytest.skip(reason)
    stack = _usr1_stack_dump(daemon_pid, quest_dir)
    pytest.fail(
        f"{what}\n"
        f"screen lines:\n" + "\n".join(screen) + "\n"
        f"events.jsonl types: {_event_types(events)}\n"
        f"daemon stack dump:\n{stack}"
    )


@pytest.mark.live("claude_code")
def test_relay_hibernate_then_resume_reengages(tmp_path: Path) -> None:
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
        # at the permanent control-root home .imp/quests/<id>/. The minimal
        # workflow never calls create_and_enter_worktree, so it runs as a draft.
        quest_dir = _await_change_dir(tmp_path, timeout=30.0)
        slug = quest_dir.name
        events = quest_dir / "events.jsonl"
        state_path = quest_dir / "state" / "state.json"
        lock_path = quest_dir / "lock"

        assert wait_for_bash_tool_call(events, timeout=120.0), "never saw bash tool_call"
        time.sleep(_CC_STARTUP_GRACE)

        # The daemon holds the flock throughout the live turn; capture its pid now
        # so a residual-failure diagnostic can SIGUSR1 it for a stack dump before
        # the SIGQUIT detach below tears it down.
        daemon_pid = int(lock_path.read_text().strip())

        # SIGINT mid-turn is the cooperative interrupt: the run yields inline to
        # the human (it does NOT pause yet — that happens on the SIGQUIT detach
        # below). The steer gate renders INTERRUPT_STEER_HINT on the pty so the
        # human can redirect or detach.
        s.send_signal(signal.SIGINT)

        # Authoritative gate-engaged check: the engine journals an
        # awaiting_user_input{interrupt: true} event the instant the steer gate
        # engages — before any socket relay or pty repaint — so it is
        # render-independent. When it never arrives the cause is imp's bash-sandbox
        # per-command overlay home failing to mount: `sleep 30` never ran, the
        # model's turn ended in ~2s, and the mid-turn SIGINT landed too late to
        # interrupt a live turn (the cooperative interrupt then times out ~2002ms
        # and escalates to a hard-kill paused). _relay_skip_or_fail SKIPs on that
        # precondition and otherwise fails with diagnostics.
        if not _poll(lambda: _interrupt_gate_engaged(events), timeout=E2E_POLL_TIMEOUT):
            _relay_skip_or_fail(
                "cooperative interrupt-steer gate never engaged "
                "(no durable awaiting_user_input{interrupt: true} event)",
                events, s.lines(), daemon_pid, quest_dir,
            )

        # The tty must still paint the hint once the gate genuinely engaged: this
        # guards the pty render path, so a real render regression fails here — now
        # distinguishable from the overlay-mount precondition handled above. That
        # repaint trails the durable gate event by an extra cross-process relay ->
        # prompt_toolkit render (3 async hops over a socket), which -n auto CPU
        # saturation can stretch past the base ceiling. Give this one render-bound
        # poll the generous E2E_RENDER_POLL_TIMEOUT budget so a late repaint under
        # load is not misread as a hang — render variance, not a hang; the 240s
        # per-test outer timeout stays the single backstop for a genuine no-paint.
        if not _poll(
            lambda: "type to redirect" in "\n".join(s.lines()).lower(),
            timeout=E2E_RENDER_POLL_TIMEOUT,
        ):
            _relay_skip_or_fail(
                "steer gate engaged (durable event present) but the pty never "
                "rendered INTERRUPT_STEER_HINT",
                events, s.lines(), daemon_pid, quest_dir,
            )

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

        assert _poll(_pid_gone, timeout=E2E_POLL_TIMEOUT), "daemon pid did not exit on detach"
        assert _poll(lambda: _flock_free(lock_path), timeout=E2E_POLL_TIMEOUT), "flock not released"
        status = wait_for_status(state_path, "paused", timeout=E2E_POLL_TIMEOUT)
        assert status == "paused", f"expected paused, got {status!r}\n{evidence(quest_dir)}"
    finally:
        s.close()

    # imp resume → a fresh daemon re-acquires the flock, appends workflow_resumed.
    r = TerminalSession(
        [sys.executable, "-m", "imp", "resume", slug],
        cwd=str(tmp_path), env=env, cols=80, rows=24,
    )
    try:
        assert _poll(
            lambda: "workflow_resumed" in _event_types(events), timeout=E2E_POLL_TIMEOUT
        ), "resume never appended workflow_resumed"
        assert _poll(
            lambda: not _flock_free(lock_path), timeout=E2E_POLL_TIMEOUT
        ), "fresh daemon never re-acquired the flock"
        fresh_pid = int(lock_path.read_text().strip())
        assert fresh_pid != daemon_pid   # a NEW daemon process owns the resume
    finally:
        r.close()
