"""Signal handling: pause-and-exit on Ctrl+C / Ctrl+D / ESC / SIGTERM.

`run_signal_receiver` uses `anyio.open_signal_receiver(SIGINT, SIGTERM)`, which
INTERCEPTS SIGINT — Python does NOT raise KeyboardInterrupt on the main thread
for it. A CPU-bound sync tool that never yields the event loop is torn down via
the process group instead; in-process tools cooperate via
`coordinator.should_pause`; HTTP providers cancel their in-flight request.

ESC is read by `EscKeyReader` (this module) during agent-thinking phases of an
interactive run: it opens /dev/tty in cbreak mode and funnels the ESC byte
(0x1b) into `InterruptCoordinator.signal_pause(reason='esc')` — the same entry
point SIGINT uses. ESC during a prompt_toolkit `Prompt.ask_async()` call is
still owned by prompt_toolkit (emacs meta-prefix / vi insert exit): the CLI
wraps that call in `coord.tty_quiesced()`, which restores the tty's original
termios for the duration of the prompt and re-arms cbreak on exit, so the two
never hold the tty in raw/cbreak mode at once.

prompt_toolkit's `prompt_async(handle_sigint=True)` installs its OWN SIGINT
handler while the interactive prompt is active and raises KeyboardInterrupt
there — handled in cli.main, separately from this coordinator.
"""

from __future__ import annotations

import contextlib
import os
import signal
from collections.abc import AsyncIterator, Iterator
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


def _reason_for_signal(sig: int) -> str:
    """Map an incoming POSIX signal number to the pause-reason string surfaced
    on the final `paused (<reason>)` headline. Unknown signals default to the
    historical `ctrl-c` so an unexpected source never blocks the pause."""
    if sig == signal.SIGTERM:
        return "sigterm"
    return "ctrl-c"


class InterruptCoordinator:
    """Signal-driven pause coordinator.

    Lifecycle:
      - CLI spawns `await coord.run_signal_receiver()` as a sibling task that
        subscribes to SIGINT/SIGTERM and calls `signal_pause(reason=...)`.
      - On interactive runs the CLI also spawns `EscKeyReader(coord).run()`,
        which calls `signal_pause(reason="esc")` on a 0x1b keystroke from
        /dev/tty.
      - The executor checks `coord.should_pause` between events; on True it
        returns RunResult.PAUSED. The CLI then reads `coord.pause_reason`
        (default "ctrl-c") to render `paused (<reason>)`.
      - Runners attach/detach an `interrupt()` callback per step; the signal
        receiver and the ESC reader both invoke it via `signal_pause`.
      - `coord.tty_quiesced()` hands /dev/tty to prompt_toolkit for the
        duration of a prompt so the ESC reader's cbreak and the prompt's raw
        mode never collide.
    """

    def __init__(self) -> None:
        self._runner_interrupt: Any = None
        self._should_pause = False
        self.pause_reason: str | None = None
        self._esc_reader: Any = None

    def attach_runner(self, interrupt_fn: Any) -> None:
        """interrupt_fn is an `async def () -> None` callable. All three V1
        runners (AnthropicAgentRunner / OpenAIAgentRunner /
        ClaudeCodeAgentRunner) declare `async def interrupt(self) -> None`
        per the AgentRunner protocol (`src/imp/agents/runner.py:172`).

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

    def attach_esc_reader(self, reader: Any) -> None:
        """Register the ESC keystroke reader so `tty_quiesced` can hand off
        termios to prompt_toolkit during interactive prompts. Unattached =
        no-op tty_quiesced (matches non-TTY / non-interactive)."""
        self._esc_reader = reader

    def detach_esc_reader(self) -> None:
        self._esc_reader = None

    @contextlib.asynccontextmanager
    async def tty_quiesced(self) -> AsyncIterator[None]:
        """Release the cbreak'd /dev/tty for the duration of a prompt_toolkit
        prompt and re-arm on exit. No-op when no reader is attached. Nesting is
        tracked on the reader, so concurrent prompt sites compose."""
        reader = self._esc_reader
        if reader is None:
            yield
            return
        async with reader.tty_quiesced():
            yield

    @property
    def should_pause(self) -> bool:
        return self._should_pause

    async def signal_pause(self, *, reason: str = "ctrl-c") -> None:
        """Fired by the signal receiver or the ESC keystroke reader. Records
        the originating `reason` (first-wins; later calls do not overwrite),
        calls the attached runner's interrupt (if any), then flips the pause
        flag. Idempotent across repeated calls."""
        fn = self._runner_interrupt
        if self.pause_reason is None:
            self.pause_reason = reason
        self._should_pause = True
        if fn is not None:
            await fn()

    async def run_signal_receiver(self) -> None:
        """Long-running task: subscribe to SIGINT/SIGTERM and call
        signal_pause for each, tagging with 'ctrl-c' or 'sigterm'.
        Cancel this task to stop receiving."""
        import anyio

        with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
            async for sig in signals:
                await self.signal_pause(reason=_reason_for_signal(sig))
