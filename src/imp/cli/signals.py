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
from collections.abc import Iterator
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
    change.event_log().append(agent_id=None, type="workflow_paused", payload={"reason": reason})


def install_process_group() -> None:
    """Become process-group leader so child subprocesses can be killed en
    masse on Ctrl+C. POSIX-only; no-op on Windows."""
    with contextlib.suppress(AttributeError, OSError):
        os.setpgrp()


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
        per the AgentRunner protocol (`src/imp/agents/runner.py:85`).

        The run-level attach/detach API; the per-drive wrapper is
        `runner_attached` (used by `drive_invocation`), which the executor and
        dispatch_task now go through instead of calling this directly."""
        self._runner_interrupt = interrupt_fn

    def detach_runner(self) -> None:
        self._runner_interrupt = None

    @contextlib.contextmanager
    def runner_attached(self, interrupt_fn: Any) -> Iterator[None]:
        """Attach `interrupt_fn` for the duration of a single drive, then
        RESTORE the previous attachment (not clear it). Nesting-safe: a nested
        subagent drive saves the parent's interrupt and reinstates it on exit,
        so ESC stays clean for the parent after a subagent returns, and ESC
        mid-subagent cancels the subagent runner."""
        prev = self._runner_interrupt
        self._runner_interrupt = interrupt_fn
        try:
            yield
        finally:
            self._runner_interrupt = prev

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
