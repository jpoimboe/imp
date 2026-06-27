"""Signal handling for the session client: two intents, cleanly separated.

The client's signals map to two distinct intents (the keep-alive-lease design):

  - Intent A — "interrupt the run" (Ctrl-C / SIGINT). Acts on the *run*: fire the
    cooperative stop (runner.interrupt()). With a human here to type, the run
    yields inline and the client stays attached; with no inline channel the stop
    lands as a clean pause.
  - Intent B — "this client is leaving" (SIGTERM, SIGHUP, terminal close,
    Ctrl-D, /save). Acts on the *attachment*: release the lease, detach, exit. The
    client does NOT decide whether the run pauses — the session daemon's lease
    rule does.

`run_signal_receiver` installs loop-level handlers via `add_signal_handler` (so
they can be RE-ARMED after a prompt_toolkit prompt deletes SIGINT's). SIGINT is
turned into `signal_pause(reason='ctrl-c')` via the receiver; the leave signals
are intercepted in `_dispatch_signal` and fire the leave (escalation) hook
directly — they never reach the receiver and never touch pause state.

The CLI calls `prompt_async(handle_sigint=False)`, so prompt_toolkit does NOT own
SIGINT during a prompt: an out-of-band SIGINT then lands in this coordinator's
loop handler. Typed Ctrl-C at a prompt is the `c-c` KEY binding (a key event,
independent of `handle_sigint`); only OS signals are governed here.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Iterator
from typing import Any

from imp.engine.change import Change
from imp.engine.state import State

_suppress = contextlib.suppress

_SUPPRESS_WINDOW_S = 1.0  # anti-double-tap: a 2nd gesture within this window is dropped

# Terminal/blocked at-rest statuses an interrupt must NOT clobber. Anything else
# (a `new` or `paused` run) becomes `paused` on interrupt.
_PRESERVE_ON_PAUSE = ("merge_blocked", "completed", "saved", "abandoned")


def _leave_signals() -> tuple[int, ...]:
    """Signals meaning 'this client is leaving' (Intent B). SIGHUP is absent on
    some platforms (Windows); include it only when present."""
    sigs: list[int] = [signal.SIGTERM]
    sighup = getattr(signal, "SIGHUP", None)
    if sighup is not None:
        sigs.append(sighup)
    return tuple(sigs)


def _handled_signals() -> tuple[int, ...]:
    """Every signal the coordinator installs a loop handler for: SIGINT (Intent A)
    plus the Intent-B leave signals."""
    return (signal.SIGINT, *_leave_signals())


def _open_pause_exists(change: Change) -> bool:
    for event in reversed(list(change.event_log().read())):
        etype = event.get("type")
        if etype == "workflow_paused":
            return True
        if etype == "workflow_resumed":
            return False
    return False


def pause_and_exit(change: Change, *, reason: str) -> None:
    """Save state as `paused` (from any active status; a terminal/blocked status
    in _PRESERVE_ON_PAUSE is left untouched) and append workflow_paused UNLESS the
    journal already holds an open pause (a workflow_paused with no intervening
    workflow_resumed). Emission-driven, NOT reason-equality: the
    clean-solicit-SIGINT (point A) and SIGTERM paths emit no engine pause, so the
    tail is not `paused` and they still append correctly."""
    try:
        state = State.load(change.state_dir)
        if state.status not in _PRESERVE_ON_PAUSE:
            state.status = "paused"
            state.save(change.state_dir)
    except FileNotFoundError:
        # State never reached the durable phase — nothing to update
        pass
    if _open_pause_exists(change):
        return  # an open pause already exists in the journal; no dup append
    change.event_log().append(
        agent_id=None, type="workflow_paused",
        payload={"reason": reason, "interrupt_reason": reason})


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
        subscribes to SIGINT (plus the Intent-B leave signals SIGTERM/SIGHUP) and
        calls `signal_pause(reason=...)`.
      - The executor checks `coord.should_pause` between events; on True it
        returns RunResult.PAUSED. The CLI then reads `coord.pause_reason`
        (default "ctrl-c") to render `paused (<reason>)`.
      - Runners attach/detach an `interrupt()` callback per step; the signal
        receiver invokes it via `signal_pause`.
    """

    def __init__(self) -> None:
        self._runner_interrupt: Any = None
        self._should_pause = False
        self.pause_reason: str | None = None
        self._loop: Any = None          # event loop owning the signal handlers
        self._sig_send: Any = None      # memory-stream send half (loop cb -> task)
        self._interjects: list[str] = []
        self._first_gesture_at: float | None = None  # monotonic; suppression window
        self.escalated = False
        self._exit_event: Any = None    # set by the exit/escalation routing (C3/C4)
        self.escalation_event: Any = None   # anyio.Event set on escalation (C4)
        self._trigger_escalation: Any = None
        self._trigger_exit: Any = None
        self._now = time.monotonic      # injectable for tests
        self._ack_hook: Any = None
        # True ONLY while an interactive prompt is being awaited (set/cleared by
        # ask_line_with_slash). During a RUN (agent thinking) this is False so an
        # in-band Ctrl-C → signal_pause → runner interrupt → pause-to-prompt is
        # unchanged. With handle_sigint=False on the prompt, an out-of-band SIGINT
        # at a prompt lands in _dispatch_signal (not as a prompt_toolkit KBI);
        # this flag tells _dispatch_signal to route it through the exit path.
        self._at_prompt = False
        # B.2 liveness: the inactivity watchdog registers a per-pull "bump"
        # callback here; `note_activity()` fires them on every shared-event-log
        # append by this agent OR any descendant. This is the ONLY liveness
        # signal that keeps flowing while a parent agent is parked in an
        # in-process subagent dispatch (claude_code yields nothing then), and it
        # is why a busy subtree no longer trips the parent's watchdog. Shared
        # subtree-wide (one coordinator per run), so a child's appends push every
        # ancestor watchdog's deadline. Same-event-loop, single-threaded => no lock.
        self._activity_listeners: list[Any] = []
        # Suspend-during-solicit (this Change): True ONLY while an attended agent
        # is blocked awaiting a human reply at the bare-turn-end gate.
        # While True the per-pull inactivity watchdog freezes its deadline to inf
        # (see agent_run._pull_with_inactivity_watchdog). Name-identical to the
        # unrelated ReplyInterjectRouter.solicit_active (reply routing) — distinct.
        self._solicit_active = False

    def add_activity_listener(self, fn: Any) -> Any:
        """Register a no-arg callback fired on each `note_activity()`. Returns
        `fn` so the watchdog can pass it back to `remove_activity_listener`."""
        self._activity_listeners.append(fn)
        return fn

    def remove_activity_listener(self, fn: Any) -> None:
        with _suppress(ValueError):
            self._activity_listeners.remove(fn)

    def note_activity(self) -> None:
        """Signal subtree liveness — wired to every shared-event-log append.
        Fires each registered watchdog bump; a failing bump (e.g. no running loop
        on some odd append path) must never break the append, so each is
        suppressed individually."""
        for fn in tuple(self._activity_listeners):
            with _suppress(Exception):
                fn()

    @property
    def solicit_active(self) -> bool:
        """True while an attended agent is blocked awaiting a human reply."""
        return self._solicit_active

    def begin_solicit_watchdog(self) -> None:
        """Enter a blocked human solicit: freeze the inactivity watchdog. Fires
        note_activity() so every registered per-pull bump re-evaluates NOW and
        freezes its in-flight deadline to inf."""
        self._solicit_active = True
        self.note_activity()

    def end_solicit_watchdog(self) -> None:
        """Leave a blocked human solicit: restore the inactivity watchdog. Fires
        note_activity() so every registered bump re-evaluates and installs a
        fresh finite window."""
        self._solicit_active = False
        self.note_activity()

    def set_at_prompt(self, value: bool) -> None:
        """Mark whether an interactive prompt is currently being awaited. A plain
        boolean, NOT depth-counted: prompts are serialized (single controlling
        tty; `router.begin_solicit`/`end_solicit`; the resume prompt runs only
        after the run loop returns), so `ask_line_with_slash` calls never nest.
        If a nested prompt site is ever added, make this depth-counted so the
        inner reset does not clear it early. Routes an out-of-band SIGINT through
        the exit path while True (see _dispatch_signal)."""
        self._at_prompt = value

    def set_ack_hook(self, fn: Any) -> None:
        """fn(text: str) renders transient stop-acknowledgment chrome in the
        status-line area (never journaled). None = no ack (tests/non-interactive)."""
        self._ack_hook = fn

    def _note_gesture_ack(self, *, force_quit_available: bool) -> None:
        fn = self._ack_hook
        if fn is None:
            return
        text = "stopping…"
        if force_quit_available:
            text += " (interrupt again to force-quit)"
        fn(text)

    def enqueue_interject(self, text: str) -> None:
        """Queue a human-typed line for delivery at the next agent turn boundary."""
        self._interjects.append(text)

    def drain_interject(self) -> str | None:
        """Pop the oldest queued interject, or None. Called at the turn boundary."""
        return self._interjects.pop(0) if self._interjects else None

    def has_queued_interject(self) -> bool:
        """True when an interject is queued for delivery (§2 resume decision)."""
        return bool(self._interjects)

    def _register_gesture(self, *, kind: str) -> bool:
        """Record an interrupt gesture and decide whether it proceeds. SIGTERM is
        exempt (a supervisor shutdown must never be suppressed). For Ctrl-C: a
        gesture within _SUPPRESS_WINDOW_S of the FIRST gesture is suppressed
        (returns False); the window is measured from the first gesture and is NOT
        restarted by suppressed presses. Returns True to proceed."""
        if kind == "sigterm":
            return True
        now = self._now()
        if self._first_gesture_at is None:
            self._first_gesture_at = now
            return True
        # Within the window → suppressed; after it → escalation territory (C3).
        # The window is NOT restarted here (no write to _first_gesture_at).
        return now - self._first_gesture_at >= _SUPPRESS_WINDOW_S

    def reset(self) -> None:
        """Clear interrupt state between session-loop rounds (§1). Runs right
        after run_change returns, before the prompt. Does NOT clear queued
        interjects."""
        self._should_pause = False
        self.pause_reason = None
        self._first_gesture_at = None
        self.escalated = False

    def clear_pause(self) -> None:
        """Consume a pause that a cooperative interrupt resolved inline. The
        Ctrl-C steer was handled within the live invocation, so the run must NOT
        return PAUSED: clear the pause flag and reason. Leaves the gesture /
        escalation window untouched so a follow-up force-quit Ctrl-C still
        escalates."""
        self._should_pause = False
        self.pause_reason = None

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

    def set_escalation_hook(self, fn: Any) -> None:
        self._trigger_escalation = fn

    def set_exit_hook(self, fn: Any) -> None:
        self._trigger_exit = fn

    def request_exit(self, reason: str) -> None:
        """Route an out-of-band stop (prompt-level SIGINT) to the exit path —
        sets pause state + fires the exit event the actuator watches."""
        if self.pause_reason is None:
            self.pause_reason = reason
        self._should_pause = True
        fn = self._trigger_exit
        if fn is not None:
            fn(reason)

    @contextlib.contextmanager
    def runner_attached(self, interrupt_fn: Any) -> Iterator[None]:
        """Attach `interrupt_fn` for the duration of a single drive, then
        RESTORE the previous attachment (not clear it). Nesting-safe: a nested
        subagent drive saves the parent's interrupt and reinstates it on exit,
        so a Ctrl-C stays clean for the parent after a subagent returns, and a
        Ctrl-C mid-subagent cancels the subagent runner."""
        prev = self._runner_interrupt
        self._runner_interrupt = interrupt_fn
        try:
            yield
        finally:
            self._runner_interrupt = prev

    @property
    def should_pause(self) -> bool:
        return self._should_pause

    async def signal_pause(self, *, reason: str = "ctrl-c") -> None:
        """Fired by the signal receiver. Records the originating `reason`
        (first-wins; later calls do not overwrite), calls the attached runner's
        interrupt (if any), then flips the pause flag. Idempotent across
        repeated calls."""
        fn = self._runner_interrupt
        if self.pause_reason is None:
            self.pause_reason = reason
            self._note_gesture_ack(force_quit_available=False)
        self._should_pause = True
        if fn is not None:
            await fn()

    def _install_loop_signal_handlers(self) -> None:
        """(Re)install the coordinator's loop signal handlers (SIGINT plus the
        Intent-B leave signals — SIGTERM/SIGHUP) on the running loop. Idempotent —
        `add_signal_handler` overwrites any existing entry.

        This is also the RE-ARM primitive — now DEFENSIVE-ONLY. With the CLI's
        `prompt_async(handle_sigint=False)` prompt_toolkit no longer installs or
        deletes a SIGINT handler, so imp's handler stays installed across a prompt
        and the re-arm is a harmless no-op. It is kept so a future provider (or a
        nested prompt) that re-enables `handle_sigint=True` still re-arms: that
        path calls `loop.remove_signal_handler(SIGINT)` on prompt exit, DELETING
        the loop's SIGINT dispatch entry (its `_restore_sigint_from_ctypes`
        restores the C-level handler, so the wakeup byte is still written — but
        with no Handle the loop dispatches nothing), which without re-arming would
        permanently and silently disable mid-run Ctrl-C. See `rearm_signal_handlers`."""
        loop = self._loop
        if loop is None:
            return
        for sig in _handled_signals():
            with _suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, self._dispatch_signal, sig)

    def _dispatch_signal(self, sig: int) -> None:
        """Loop-thread signal callback, split by the two client intents.

        Intent B — "this client is leaving" (the leave signals): fire the leave
        (escalation) hook so the client releases its lease, detaches, and exits.
        The client does NOT decide whether the run pauses — the daemon's lease
        rule does — so this path never touches pause state and is never suppressed.

        Intent A — "interrupt the run" (SIGINT): if a stop is already unwinding,
        apply the suppression/escalation gate HERE (signal_pause can wedge on the
        cancellation-immune interrupt()); at a prompt, route through the exit path;
        otherwise enqueue for the receiver (→ signal_pause → runner interrupt →
        pause-to-prompt)."""
        if sig in _leave_signals():
            fn = self._trigger_escalation
            if fn is not None:
                fn()
            return
        if self._should_pause:
            # A stop is already unwinding. Gate the repeat gesture synchronously.
            if self._register_gesture(kind="ctrl-c"):
                self._note_gesture_ack(force_quit_available=True)
                esc = self._trigger_escalation
                if esc is not None:
                    esc()
            return  # suppressed or escalated — never re-enter a wedged interrupt()
        if self._at_prompt:
            # At an interactive prompt, prompt_toolkit no longer owns SIGINT
            # (handle_sigint=False), so an out-of-band SIGINT lands here. Route it
            # through the coordinator exit path. Register the gesture first so a
            # SECOND out-of-band SIGINT still hits the escalation/suppression gate
            # above (force-quit).
            self._register_gesture(kind="ctrl-c")
            self.request_exit("ctrl-c")
            return
        # First gesture during a run: record it and enqueue for the receiver.
        self._register_gesture(kind="ctrl-c")
        send = self._sig_send
        if send is None:
            return
        with _suppress(Exception):  # WouldBlock (buffer full) / closed stream
            send.send_nowait(sig)

    def rearm_signal_handlers(self) -> None:
        """Re-install the loop SIGINT/SIGTERM/SIGHUP handlers after a prompt_toolkit
        prompt deleted SIGINT's on exit. Safe no-op before the receiver starts or
        after it stops (`_loop is None`). Called from the interactive-prompt choke
        point (`ask_line_with_slash`) so mid-run Ctrl-C survives every prompt."""
        self._install_loop_signal_handlers()

    async def run_signal_receiver(self) -> None:
        """Long-running task: install the coordinator's loop signal handlers.
        SIGINT (Intent A) is turned into `signal_pause(reason='ctrl-c')` via this
        receiver. The leave signals (Intent B) are intercepted in
        `_dispatch_signal` BEFORE the stream — they fire the leave (escalation)
        hook directly and never reach this loop.

        Uses directly-managed loop handlers (not `anyio.open_signal_receiver`) so
        they can be RE-ARMED (`rearm_signal_handlers`) after prompt_toolkit
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
                for sig in _handled_signals():
                    with _suppress(NotImplementedError, RuntimeError, ValueError):
                        loop.remove_signal_handler(sig)
            self._loop = None
            self._sig_send = None
            send.close()
