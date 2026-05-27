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
from typing import Any

from imp.engine.change import Change
from imp.engine.state import State

_suppress = contextlib.suppress


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
    with contextlib.suppress(AttributeError, OSError):
        os.setpgrp()


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


class InterruptCoordinator:
    """Signal-driven pause coordinator (spec §6.15).

    Lifecycle:
      - CLI spawns `await coord.run_signal_receiver(scope)` as a sibling task.
      - The receiver subscribes to SIGINT and SIGTERM and calls
        `await coord.signal_pause()` for each.
      - The executor checks `coord.should_pause` between events; on True,
        returns RunResult.PAUSED.
      - Runners attach/detach an `interrupt()` callback per step; the
        receiver calls it during signal_pause.

    Why no ESC: ESC is per-keystroke from prompt_toolkit (raw mode); it never
    travels through OS signals. The interactive prompt path in
    cli/prompt.py handles ESC directly via the ESC_SENTINEL.
    """

    def __init__(self) -> None:
        self._runner_interrupt: Any = None
        self._should_pause = False

    def attach_runner(self, interrupt_fn: Any) -> None:
        """interrupt_fn is an `async def () -> None` callable. All three V1
        runners (AnthropicAgentRunner / OpenAIAgentRunner /
        ClaudeCodeAgentRunner) declare `async def interrupt(self) -> None`
        per the AgentRunner protocol (`src/imp/agents/runner.py:85`), so the
        existing `coordinator.attach_runner(runner.interrupt)` call site in
        executor.py works unchanged — no sync→async migration required."""
        self._runner_interrupt = interrupt_fn

    def detach_runner(self) -> None:
        self._runner_interrupt = None

    @property
    def should_pause(self) -> bool:
        return self._should_pause

    async def signal_pause(self) -> None:
        """Fired by the signal receiver. Calls the attached runner's
        interrupt (if any) then flips the pause flag. Idempotent across
        repeated calls."""
        fn = self._runner_interrupt
        self._should_pause = True
        if fn is not None:
            await fn()

    async def run_signal_receiver(self) -> None:
        """Long-running task: subscribe to SIGINT/SIGTERM and call
        signal_pause for each. Cancel this task to stop receiving."""
        import anyio

        with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
            async for _sig in signals:
                await self.signal_pause()


def _signal_pg_excluding_self(pgid: int, sig: int) -> None:
    """Walk /proc looking for procs whose PGID matches; signal those that
    aren't us. POSIX-only; best-effort."""
    # Runs only on interrupt, so O(n_procs) /proc walk is acceptable.
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
