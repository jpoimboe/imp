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

import asyncio
import contextlib
import os
import signal
from collections.abc import AsyncIterator, Iterator
from typing import Any

from anyio import TASK_STATUS_IGNORED
from anyio.abc import TaskStatus

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
        self._loop: Any = None          # event loop owning the signal handlers
        self._sig_send: Any = None      # memory-stream send half (loop cb -> task)
        self._interjects: list[str] = []

    def enqueue_interject(self, text: str) -> None:
        """Queue a human-typed line for delivery at the next agent turn boundary."""
        self._interjects.append(text)

    def drain_interject(self) -> str | None:
        """Pop the oldest queued interject, or None. Called at the turn boundary."""
        return self._interjects.pop(0) if self._interjects else None

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
        tracked on the reader, so nested prompt sites compose."""
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

    def _install_loop_signal_handlers(self) -> None:
        """(Re)install SIGINT/SIGTERM handlers on the running loop. Idempotent —
        `add_signal_handler` overwrites any existing entry.

        This is also the RE-ARM primitive. prompt_toolkit's
        `prompt_async(handle_sigint=True)` calls `loop.remove_signal_handler(
        SIGINT)` when the prompt exits, which DELETES the loop's SIGINT dispatch
        entry. (Its `_restore_sigint_from_ctypes` restores the C-level handler,
        so the wakeup byte is still written — but with no Handle the loop reads
        the byte and dispatches nothing.) Without re-arming, the FIRST interactive
        prompt permanently and silently disables mid-run Ctrl-C — the runner's
        `coord.signal_pause` is never called again, so `_cancel`/`should_pause`
        never flip and a process-group Ctrl-C surfaces as a codex-exec error
        classified `unknown`. See `rearm_signal_handlers`."""
        loop = self._loop
        if loop is None:
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            with _suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, self._dispatch_signal, sig)

    def _dispatch_signal(self, sig: int) -> None:
        """Loop-thread signal callback: enqueue the signal for the receiver task
        (which awaits `signal_pause`). Keeping the await in the task — rather than
        firing `signal_pause` as a bare task from here — preserves structured
        concurrency and the first-wins reason semantics."""
        send = self._sig_send
        if send is None:
            return
        with _suppress(Exception):  # WouldBlock (buffer full) / closed stream
            send.send_nowait(sig)

    def rearm_signal_handlers(self) -> None:
        """Re-install the loop SIGINT/SIGTERM handlers after a prompt_toolkit
        prompt deleted SIGINT's on exit. Safe no-op before the receiver starts or
        after it stops (`_loop is None`). Called from the interactive-prompt choke
        point (`ask_line_with_slash`) so mid-run Ctrl-C survives every prompt."""
        self._install_loop_signal_handlers()

    async def run_signal_receiver(self) -> None:
        """Long-running task: install SIGINT/SIGTERM loop handlers and turn each
        delivered signal into `signal_pause` (tagged 'ctrl-c'/'sigterm').

        Replaces `anyio.open_signal_receiver` with directly-managed loop handlers
        so they can be RE-ARMED (`rearm_signal_handlers`) after prompt_toolkit
        prompts delete the SIGINT handler. Cancel this task to stop receiving."""
        import anyio

        self._loop = asyncio.get_running_loop()
        send, recv = anyio.create_memory_object_stream[int](8)
        self._sig_send = send
        self._install_loop_signal_handlers()
        try:
            async with recv:
                async for sig in recv:
                    await self.signal_pause(reason=_reason_for_signal(sig))
        finally:
            loop = self._loop
            if loop is not None:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    with _suppress(NotImplementedError, RuntimeError, ValueError):
                        loop.remove_signal_handler(sig)
            self._loop = None
            self._sig_send = None
            send.close()


_READ_TIMEOUT_S = 0.05  # ESC-during-thinking latency + prompt-entry park-wait ceiling


class EscKeyReader:
    """POSIX `/dev/tty` keystroke reader. Opens the controlling terminal in
    cbreak mode and funnels a single ESC byte (0x1b) into
    `coord.signal_pause(reason='esc')` — the same entry point SIGINT uses.

    `coord.tty_quiesced()` hands the terminal to prompt_toolkit for a prompt: it
    parks the reader (waits for the reader to acknowledge, via `_parked_evt`,
    that it has stopped reading /dev/tty) BEFORE restoring termios and yielding,
    then re-arms cbreak on exit (depth-counted, nestable). Because the park is
    synchronous, the reader and prompt_toolkit never both drive the controlling
    terminal — no stolen first keystroke, no spurious paused(esc) during a
    prompt (AC #3). Entering a prompt therefore waits up to one read timeout
    (`_READ_TIMEOUT_S`) for the in-flight read to return; an ESC arriving in that
    pre-park window is still "during thinking" (the prompt is not shown yet), so
    pausing then is correct. The reader's `finally` also sets `_parked_evt`, so a
    reader that exited (ESC already fired) releases a waiting quiescer instead of
    deadlocking. On EBADF/OSError (no controlling tty — CI, piped stdin) `run()`
    returns cleanly and stays unarmed: ESC becomes a no-op. Windows has no
    `termios`/`tty`; `run()` exits cleanly there too.

    Caveats: (1) in cbreak a bare ESC is also the prefix of arrow-key sequences
    (up-arrow = ESC [ A). The reader fires on the first ESC byte and exits, so
    an arrow press during agent-thinking pauses the run — there is no prompt
    active then, so the spec accepts this. (2) Startup window: `tty_quiesced` is
    a no-op until `run()` opens /dev/tty and attaches, so a prompt reached
    before the reader arms gets no hand-off; in practice the first agent turn
    thinks for seconds before any prompt, so the reader is armed long before
    (accepted scope, not guarded). (3) An abandoned read thread (cancellation +
    the `_READ_TIMEOUT_S` select) can wake and `os.read` a just-closed fd —
    caught as OSError; only if that fd number were reused in-process within that
    window could it consume one byte from the wrong fd (vanishingly unlikely).
    (4) A SIGKILL/panic past the `finally` leaves the terminal in cbreak (echo
    off) — inherent to any raw-mode TUI; this always-on reader widens that
    window slightly."""

    def __init__(
        self, coord: InterruptCoordinator,
        *, on_interject: Any = None,
    ) -> None:
        self._coord = coord
        self._fd: int | None = None
        self._orig_attrs: Any = None
        self._armed = False
        self._quiesce_depth = 0
        self._unquiesce_evt: Any = None        # anyio.Event | None — wakes parked reader
        self._parked_evt: Any = None           # anyio.Event — reader → quiescer ack
        # Task 11: optional `(line: str) -> None` sink for interject lines typed
        # during agent-thinking. None (default) = byte-identical legacy behavior:
        # the reader fires ONLY on ESC and ignores every other keystroke. When a
        # sink is set, printable bytes accumulate into a line that is delivered on
        # Enter (routed to the interject queue by the CLI). ESC still pauses
        # immediately and discards any partial line. This is the sole /dev/tty
        # owner during thinking, so it cannot collide with the ESC watch (one
        # reader, one terminal); during a solicit the reader is quiesced and
        # prompt_toolkit owns input → the line is a reply, not an interject.
        self._on_interject = on_interject
        self._line_buf = bytearray()

    def set_interject_sink(self, sink: Any) -> None:
        """Attach (or replace) the `(line: str) -> None` interject sink after
        construction. None restores legacy ESC-only behavior."""
        self._on_interject = sink

    async def run(self, *, task_status: TaskStatus[None] = TASK_STATUS_IGNORED) -> None:
        # `task_status.started()` is signalled once the reader has either armed or
        # conclusively given up, so `await tg.start(reader.run)` lets the caller
        # block until then. This closes the startup race: a workflow whose first
        # step is an interactive prompt would otherwise quiesce against an
        # unattached reader (a no-op), let prompt_toolkit set up the terminal, and
        # then arm mid-prompt — clobbering the prompt's input mode with cbreak.
        try:
            import termios
            import tty
        except ImportError:
            task_status.started()
            return                              # Windows / no termios
        try:
            self._fd = os.open(
                "/dev/tty", os.O_RDONLY | getattr(os, "O_NOCTTY", 0))
        except OSError:
            task_status.started()
            return                              # no controlling tty
        try:
            try:
                self._orig_attrs = termios.tcgetattr(self._fd)
                tty.setcbreak(self._fd)
            except termios.error:
                task_status.started()
                return                          # not a tty after all
            import anyio
            self._parked_evt = anyio.Event()
            self._armed = True
            self._coord.attach_esc_reader(self)
            task_status.started()               # armed: prompts may now start

            while True:
                if self._quiesce_depth > 0:
                    # Park: stop reading /dev/tty, ACK the quiescer, wait to resume.
                    if self._unquiesce_evt is None:
                        self._unquiesce_evt = anyio.Event()
                    self._parked_evt.set()
                    await self._unquiesce_evt.wait()
                    self._unquiesce_evt = None
                    continue
                byte = await anyio.to_thread.run_sync(
                    self._read_one_byte, abandon_on_cancel=True)
                if byte == b"\x1b":
                    self._line_buf.clear()       # ESC discards any partial line
                    await self._coord.signal_pause(reason="esc")
                    return
                # Empty bytes (select timeout / quiesce race) → loop.
                if byte and self._on_interject is not None:
                    self._feed_interject_byte(byte)
        finally:
            self._armed = False
            self._coord.detach_esc_reader()
            if self._parked_evt is not None:
                self._parked_evt.set()      # release any quiescer awaiting a park ack
            if self._fd is not None and self._orig_attrs is not None:
                try:
                    import termios
                except ImportError:
                    pass
                else:
                    with contextlib.suppress(OSError, termios.error):
                        termios.tcsetattr(
                            self._fd, termios.TCSADRAIN, self._orig_attrs)
            if self._fd is not None:
                with contextlib.suppress(OSError):
                    os.close(self._fd)
                self._fd = None

    def _read_one_byte(self) -> bytes:
        """Blocking read of one byte with a short select timeout so the loop can
        periodically observe quiesce state. Returns b'' on timeout or any
        non-fatal read error."""
        import select
        fd = self._fd
        if fd is None:
            return b""
        try:
            ready, _, _ = select.select([fd], [], [], _READ_TIMEOUT_S)
        except (OSError, ValueError):
            return b""
        if not ready:
            return b""
        if self._quiesce_depth > 0:
            # A prompt began quiescing while this read was in flight. The pending
            # byte belongs to prompt_toolkit, not us — leave it in the tty buffer
            # so the prompt reads it after termios is restored. Consuming it here
            # is the `ESC_READER_ATE: b'1'` race: the prompt would then see only
            # the trailing `\r`, read empty input, and re-prompt forever.
            return b""
        try:
            return os.read(fd, 1)
        except OSError:
            return b""

    def _feed_interject_byte(self, byte: bytes) -> None:
        """Accumulate a typed byte into the in-progress interject line; on Enter
        deliver the line to `_on_interject` and reset. Minimal raw line editing
        (backspace handling + echo) — the heavy prompt_toolkit editing stays on
        the solicit/reply path. Only reached when an interject sink is attached
        and the reader is actively reading (not quiesced)."""
        if byte in (b"\r", b"\n"):
            line = self._line_buf.decode("utf-8", errors="replace")
            self._line_buf.clear()
            with _suppress(OSError):
                os.write(1, b"\r\n")            # echo the newline
            sink = self._on_interject
            if sink is not None:
                sink(line)
            return
        if byte in (b"\x7f", b"\x08"):          # DEL / BS
            if self._line_buf:
                self._line_buf.pop()
                with _suppress(OSError):
                    os.write(1, b"\b \b")       # erase one echoed char
            return
        # Ignore other control bytes (e.g. a stray arrow-key tail); echo printable.
        if byte < b"\x20":
            return
        self._line_buf.extend(byte)
        with _suppress(OSError):
            os.write(1, byte)                   # echo so the user sees their typing

    @contextlib.asynccontextmanager
    async def tty_quiesced(self) -> AsyncIterator[None]:
        """Release termios for a prompt_toolkit prompt and re-arm cbreak on
        exit. Idempotent under nesting (depth-counted)."""
        if not self._armed or self._fd is None or self._orig_attrs is None:
            yield
            return
        try:
            import termios
            import tty
        except ImportError:
            yield
            return
        self._quiesce_depth += 1
        if self._quiesce_depth == 1:
            # Wait until the reader has actually stopped reading /dev/tty BEFORE
            # handing the terminal to prompt_toolkit — no shared-tty window, so no
            # stolen keystroke and no spurious paused(esc) during a prompt (AC #3).
            # The reader's finally also sets _parked_evt, so a reader that exited
            # (ESC already fired) releases us instead of deadlocking.
            if self._parked_evt is not None:
                await self._parked_evt.wait()
            if self._fd is not None:   # reader may have closed it (ESC fired / cancel)
                with contextlib.suppress(OSError, termios.error):
                    termios.tcsetattr(
                        self._fd, termios.TCSADRAIN, self._orig_attrs)
        try:
            yield
        finally:
            self._quiesce_depth -= 1
            if self._quiesce_depth == 0:
                if self._fd is not None:   # guard setcbreak(None) → TypeError (not suppressed)
                    with contextlib.suppress(OSError, termios.error):
                        tty.setcbreak(self._fd)
                import anyio
                self._parked_evt = anyio.Event()   # fresh ack for the next cycle
                evt = self._unquiesce_evt
                if evt is not None:
                    evt.set()
