"""Signal handling: pause-and-exit on Ctrl+C / Ctrl+D / SIGTERM.

The executor naturally yields control between events (it iterates the runner's
generator and writes the log). Catching KeyboardInterrupt at the outer
_cmd_new/_cmd_resume frame is sufficient — Python's default SIGINT raises in
the main thread on the next bytecode boundary, interrupting whatever runner
code is in flight (including blocking I/O).

Ctrl+D arrives from prompt_toolkit as EOFError. Handled identically.

Tool-kill escalation (Task 13) happens via the process-group mechanism
installed when imp starts.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time

from imp.engine.change import Change
from imp.engine.state import State

_suppress = contextlib.suppress

# Mid-stream ESC (cancel an LLM call in flight) is post-V0. The langchain
# cancellation token API in the installed version doesn't surface cleanly
# through deepagents' stream interface; punt to Plan 4. V0 ESC only fires
# between turns (when ChatMiddleware is awaiting user input).


def pause_and_exit(change: Change, *, reason: str) -> None:
    """Save state as paused; append workflow_paused with reason. Idempotent."""
    try:
        state = State.load(change.state_dir)
        state.status = "paused"
        state.save(change.state_dir)
    except FileNotFoundError:
        # State never reached the durable phase — nothing to update
        pass
    change.event_log().append("workflow_paused", {"reason": reason})


def install_process_group() -> None:
    """Become process-group leader so child subprocesses can be killed en
    masse on Ctrl+C. POSIX-only; no-op on Windows."""
    try:
        os.setpgrp()
    except (AttributeError, OSError):
        # Windows or already a leader; non-fatal
        pass


def tool_kill_escalation(
    *, timeout: float = 2.0, kill_self: bool = True,
) -> None:
    """Send SIGTERM to our PG, wait `timeout`, then SIGKILL anything alive.

    If kill_self is False, send to every member of the PG EXCEPT us — used
    by tests so pytest itself doesn't die. The CLI always calls with the
    default (kill_self=True) — by then we've already saved state and are
    about to sys.exit.
    """
    if not hasattr(os, "killpg"):
        return  # Windows or other non-POSIX
    pgid = os.getpgrp()
    if kill_self:
        with _suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGTERM)
    else:
        _signal_pg_excluding_self(pgid, signal.SIGTERM)
    time.sleep(timeout)
    if kill_self:
        with _suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
    else:
        _signal_pg_excluding_self(pgid, signal.SIGKILL)


def _signal_pg_excluding_self(pgid: int, sig: int) -> None:
    """Walk /proc looking for procs whose PGID matches; signal those that
    aren't us. POSIX-only; best-effort."""
    self_pid = os.getpid()
    try:
        entries = os.listdir("/proc")
    except FileNotFoundError:  # non-Linux POSIX (macOS) — skip in this path
        return
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == self_pid:
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                # Field 5 is pgid; format: pid (comm) state ppid pgid ...
                # comm may contain spaces, so parse from the closing paren
                stat = f.read()
            close_paren = stat.rfind(")")
            fields = stat[close_paren + 2:].split()
            if int(fields[2]) == pgid:
                with _suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, sig)
        except (FileNotFoundError, OSError, IndexError, ValueError):
            continue
