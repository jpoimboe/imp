# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from imp.agents.skills import SkillError
from imp.agents.transcript import ContinuationNote, SkillReply, SolicitReply
from imp.engine.state import JsonlEventLog
from imp.remote.session.protocol import (
    CONTROL_ABANDON,
    CONTROL_ARCHIVE,
    CONTROL_COMPACT,
    CONTROL_DETACH,
    CONTROL_DISCARD,
    CONTROL_INTERRUPT,
    CONTROL_INTERRUPT_FORCE,
    CONTROL_INTERRUPT_LEAVE,
    CONTROL_RETRY_NOW,
    CONTROL_SAVE,
    CONTROL_SET_MODEL,
    CONTROL_SET_PROVIDER,
    CONTROL_SET_SANDBOX,
    LEAVE_DISPOSITIONS,
    SIDE_EFFECT_REASONS,
    EndOfRun,
    SkillRequest,
)
from imp.remote.session.socket_server import (
    AttachRefused,
    DropReason,
    InputPayload,
    ReplaySnapshot,
    SessionSocket,
)

_log = logging.getLogger("imp.remote.session.server")

RunFn = Callable[..., Awaitable[Any]]

_CONTROL_QUEUE_DEPTH = 64
_INTERNAL_PAUSE = "__detach_pause__"  # server-internal: never a client frame action
_DISPOSITION_PRECEDENCE = (CONTROL_SAVE, CONTROL_ARCHIVE, CONTROL_DISCARD)

# Emitted from the CONTROL_SET_SANDBOX handler as script_output scrollback, mirroring
# _run_compaction's "compacted — …". Wording is byte-identical to the former client copy.
SANDBOX_OFF_CONFIRMED = (
    "Sandbox off — this step is now unsandboxed. It re-enables when the "
    "step finishes; run /sandbox on to re-enable it now.")
SANDBOX_ON_FROM_OFF = (
    "Sandbox on. Shell and file tools are confined to the worktree again. "
    "Processes already started while unsandboxed keep their current access.")
# Injected into the parked solicit on `off`, so the step that asked for it resumes itself.
SANDBOX_OFF_NOTE = "The user has disabled the sandbox."

# Verbatim drop copy; {buffer} is the per-client queue cap the viewer passed.
_DROP_COPY: dict[DropReason, tuple[str, str]] = {
    DropReason.LAG_OVERFLOW: (
        "a viewer fell behind and was disconnected",
        "its event queue passed {buffer} pending events"),
    DropReason.WRITE_FAILURE: (
        "a viewer's connection broke and was disconnected",
        "the socket write failed"),
}

# Startup grace: how long a hibernation gate waits for the FIRST client to attach
# before falling back to hibernation. The spawning client (interactive or remote
# reply delivery) attaches a beat after the daemon starts resuming, so a
# bare-turn-end solicit can reach the gate first; without this wait it would
# hibernate on the transient zero-client window and the resume would pause+exit.
# Generous vs. attach latency (which loses by ~tens of ms); only a crashed client
# ever exhausts it.
_DEFAULT_FIRST_ATTACH_TIMEOUT = 30.0


@dataclass(frozen=True)
class LeftAt:
    """Where the run was at the session's first leave: the step, and the prompt (None mid-run)."""
    prompt: dict[str, Any] | None
    step: str | None


class SessionServer:
    def __init__(
        self,
        socket_path: str,
        *,
        coordinator: Any = None,
        on_abandon: Callable[[], None] | None = None,
        lock: Any = None,
        hibernate: bool = False,
        events_path: Path | None = None,
        on_first_attach: Callable[[], None] | None = None,
        first_attach_timeout: float = _DEFAULT_FIRST_ATTACH_TIMEOUT,
        skill_loader: Callable[[str], str] | None = None,
        on_query: Callable[[str, dict[str, Any]], Any] | None = None,
        set_provider: Callable[[str], None] | None = None,
        current_step: Callable[[], str | None] = lambda: None,
    ) -> None:
        self._coordinator = coordinator
        self._on_abandon = on_abandon
        self._lock = lock
        self._hibernate = hibernate
        self._events_path = events_path
        # Plain handle: appending through the broadcast-wired log re-enters _send_one.
        self._drop_log: JsonlEventLog | None = (
            JsonlEventLog(events_path) if events_path is not None else None)
        self._on_first_attach = on_first_attach
        self._first_attach_timeout = first_attach_timeout
        self._skill_loader = skill_loader
        self._set_provider = set_provider
        self._current_step = current_step
        # Set on the first 0 -> 1 LEASED attach and never cleared: distinguishes
        # "no keep-alive client has arrived yet" (startup — wait) from "a
        # keep-alive client attached and left" (hibernate now) at the
        # zero-leased gates.
        self._first_leased_attach_fired: bool = False
        self.leave: str | None = None   # the leave control that ended the run
        self.left_at: LeftAt | None = None
        self.save_reason: str | None = None
        self._abandon_requested = False
        self._dispositions: dict[str, str | None] = {}
        self._run_scope: anyio.CancelScope | None = None
        self._pending_prompt: dict[str, Any] | None = None
        self._last_gate_event: dict[str, Any] | None = None
        self._in_wait_for_turn = False
        # Initialized in open() because anyio.Event needs a running event loop.
        self._attach_changed: anyio.Event | None = None
        self._side_effect_pending: anyio.Event | None = None
        self._socket = SessionSocket(
            socket_path,
            on_attach=self._on_attach_pulse,
            on_detach=self._on_detach_pulse,
            on_control=self._enqueue_control,
            on_lease_change=self._on_lease_change,
            on_query=on_query,
            on_drop=self._record_client_drop,
        )
        ctl_send, ctl_recv = anyio.create_memory_object_stream[tuple[str, str | None]](
            max_buffer_size=_CONTROL_QUEUE_DEPTH)
        self._control_send: MemoryObjectSendStream[tuple[str, str | None]] = ctl_send
        self._control_recv: MemoryObjectReceiveStream[tuple[str, str | None]] = ctl_recv
        if coordinator is not None:
            coordinator.set_save_hook(self.request_save)

    @property
    def attached_count(self) -> int:
        return self._socket.attached_count

    @property
    def leased_count(self) -> int:
        return self._socket.leased_count

    @property
    def abandoned(self) -> bool:
        return self.leave == CONTROL_ABANDON

    @property
    def saved(self) -> bool:
        return self.leave == CONTROL_SAVE

    @property
    def archived(self) -> bool:
        return self.leave == CONTROL_ARCHIVE

    @property
    def discarded(self) -> bool:
        return self.leave == CONTROL_DISCARD

    async def open(self) -> None:
        self._attach_changed = anyio.Event()
        self._side_effect_pending = anyio.Event()
        await self._socket.open()

    async def serve(self, run_fn: RunFn) -> Any:
        # Releasing on return is the "release lock" half of hibernation.
        if self._lock is None:
            return await self._serve(run_fn)
        with self._lock.acquire():
            return await self._serve(run_fn)

    async def _serve(self, run_fn: RunFn) -> Any:
        result: Any = None
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._socket.serve)
                # The run reached its own conclusion — returned, or raised. A leave
                # control cancels only run_tg, so the client pumps outlive the run
                # and the end frame still reaches every client, naming the leave.
                concluded = False
                try:
                    async with anyio.create_task_group() as run_tg:
                        self._run_scope = run_tg.cancel_scope
                        if self._coordinator is not None:
                            self._coordinator.set_delivery_spawner(run_tg.start_soon)
                        run_tg.start_soon(self._control_loop)
                        try:
                            result = await run_fn(
                                on_event=self._emit, user_chat_input_fn=self._chat)
                            concluded = True
                        except anyio.get_cancelled_exc_class():
                            raise
                        except BaseException:
                            concluded = True
                            raise
                        finally:
                            if self._coordinator is not None:
                                self._coordinator.clear_delivery_spawner()
                            await self._control_send.aclose()
                finally:
                    if concluded or self.leave is not None:
                        # Queued behind every pending event, then drained by the same
                        # flush, so it is provably the last frame each client receives.
                        self._socket.enqueue_end(EndOfRun(self.leave))
                        await self._socket.flush_pending()
                tg.cancel_scope.cancel()
        finally:
            await self._socket.close()
        return result

    def _emit(self, event: dict[str, Any]) -> None:
        if event.get("type") == "awaiting_user_input":
            self._last_gate_event = event
        self._socket.broadcast(event)

    def _record_client_drop(self, reason: DropReason) -> None:
        # A terminal run rmtrees the home before this can fire; mkdir would resurrect it.
        if self._drop_log is None or not self._drop_log.path.parent.is_dir():
            return
        message, detail = _DROP_COPY[reason]
        self._drop_log.append(
            agent_id=None, type="runner_warning",
            payload={"message": message,
                     "detail": detail.format(buffer=self._socket.client_buffer)})

    async def _chat(self, _query: str) -> SolicitReply:
        self.set_pending_prompt(self._last_gate_event)
        # Arm the input latch for the whole solicit: a reply arriving between the
        # prompt and next_input() opening the gate is latched, not dropped.
        self._socket.arm_input()
        try:
            got = (await self._hibernation_gate() if self._hibernate
                   else await self._socket.next_input())
            self.left_at = None   # answered: the session carried on past any leave
            self._emit({"type": "user_input_resolved", "payload": {}})
            if isinstance(got, SkillRequest):
                assert self._skill_loader is not None
                command = f"/{got.name} {got.args}".rstrip()
                try:
                    body = self._skill_loader(got.name)
                except SkillError:
                    return command
                body_with_args = (
                    f"{body.rstrip()}\n\n{got.args}\n" if got.args else body)
                return SkillReply(
                    command=command, name=got.name, body=body_with_args)
            return got
        finally:
            self.set_pending_prompt(None)
            self._last_gate_event = None
            self._socket.disarm_input()

    async def _await_first_attach(self) -> bool:
        """Startup-only: block until the first LEASED client attaches, bounded by
        first_attach_timeout. Returns True iff a leased client is attached when it
        stops waiting (False on timeout). Captures the attach event BEFORE
        testing leased_count so an attach landing between test and await is
        not a lost wakeup."""
        with anyio.move_on_after(self._first_attach_timeout):
            while True:
                evt = self._attach_changed
                if self._socket.leased_count > 0:
                    return True
                if evt is None:                  # pre-open(): no event to await
                    await anyio.sleep(0.02)
                else:
                    await evt.wait()
        return self._socket.leased_count > 0

    async def _hibernate_or_await_first_attach(self) -> None:
        """Resolve a zero-leased gate. A keep-alive client that already attached
        and left (_first_leased_attach_fired) hibernates now; otherwise this is
        the startup window before the spawning keep-alive client lands — wait
        for it, and only hibernate if it never shows."""
        from imp.engine.awaiting_user_input import AwaitingUserInput

        if self._first_leased_attach_fired or not await self._await_first_attach():
            raise AwaitingUserInput()

    async def _hibernation_gate(self) -> InputPayload:
        from imp.engine.awaiting_user_input import AwaitingUserInput

        holder: dict[str, InputPayload] = {}
        async with anyio.create_task_group() as tg:
            async def _await_input() -> None:
                holder["text"] = await self._socket.next_input()
                tg.cancel_scope.cancel()

            async def _await_hibernate() -> None:
                await self._wait_until_unleased()
                self._latch_leave()
                tg.cancel_scope.cancel()

            async def _await_side_effect() -> None:
                await self._wait_for_side_effect()
                tg.cancel_scope.cancel()

            tg.start_soon(_await_input)
            tg.start_soon(_await_hibernate)
            tg.start_soon(_await_side_effect)
        if "text" not in holder:
            raise AwaitingUserInput()
        return holder["text"]

    async def _wait_until_unleased(self) -> None:
        """Resolve when the run should hibernate: no keep-alive lease remains after
        the startup grace (a leased client has fired, or the first-leased-attach
        timeout lapsed with none). Loops while a lease is held so a re-leased
        attach re-arms it. Runs CONCURRENTLY with next_input(), so a lease-less
        client's injected reply (the remote deliver-reply path) is still consumed
        before this resolves — leases gate KEEP-ALIVE, not the input channel."""
        while True:
            evt = self._attach_changed
            if self._socket.leased_count == 0:
                if self._first_leased_attach_fired:
                    return
                if not await self._await_first_attach():
                    return
                continue   # a leased attach arrived; re-evaluate (now leased > 0)
            if evt is None:                  # pre-open(): no event to await
                await anyio.sleep(0.02)
            else:
                await evt.wait()

    def _pulse_attach_change(self) -> None:
        evt = self._attach_changed
        if evt is not None:
            evt.set()
            self._attach_changed = anyio.Event()

    def _pulse_side_effect(self) -> None:
        evt = self._side_effect_pending
        if evt is not None:
            evt.set()
            self._side_effect_pending = anyio.Event()

    async def _wait_for_side_effect(self) -> None:
        """Resolve when a side-effect pause is pending on the coordinator. The
        between-turns solicit stays attached and leased, so the unlease arm never
        fires; this arm supplies the concrete cancel that unwinds the parked
        next_input() to AwaitingUserInput without a detach. Snapshots the event
        before testing the predicate so a frame arriving between the check and the
        await is not a lost wakeup; the predicate reads committed coordinator
        state so the wake is level-triggered (fires whether the frame lands before
        or after gate entry)."""
        while True:
            evt = self._side_effect_pending
            if (self._coordinator is not None
                    and self._coordinator.should_pause
                    and self._coordinator.pause_reason in SIDE_EFFECT_REASONS):
                return
            if evt is None:                  # pre-open(): no event to await
                await anyio.sleep(0.02)
            else:
                await evt.wait()

    async def wait_for_attach_change(self) -> None:
        """Wait for the next attach or detach event."""
        evt = self._attach_changed
        if evt is not None:
            await evt.wait()

    async def wait_for_turn(
        self, predicate: Callable[[], bool], *, poll_interval: float = 2.0,
    ) -> None:
        self._in_wait_for_turn = True
        try:
            while True:
                if predicate():
                    return
                if self._socket.leased_count == 0:
                    # Same startup vs. detach split as the solicit gate: wait for the
                    # first attach before hibernating so a turn-wait reached during
                    # the startup window doesn't pause+exit.
                    await self._hibernate_or_await_first_attach()
                with anyio.move_on_after(poll_interval):
                    await self.wait_for_attach_change()
        finally:
            self._in_wait_for_turn = False

    def _latch_leave(self) -> None:
        if self.left_at is None:
            self.left_at = LeftAt(self._pending_prompt, self._current_step())

    def set_pending_prompt(self, event: dict[str, Any] | None) -> None:
        """Track a prompt event to re-emit to clients that connect after it fires."""
        self._pending_prompt = event

    def _on_attach_pulse(
        self, _attach: Any, send_one: Callable[[dict[str, Any]], None],
    ) -> ReplaySnapshot | AttachRefused | None:
        snapshot: ReplaySnapshot | None = None
        if self._events_path is not None:
            try:
                snapshot = ReplaySnapshot.capture(self._events_path)
            except FileNotFoundError:
                return AttachRefused.HISTORY_GONE  # a terminal run reaped the home
        self._pulse_attach_change()
        if self._pending_prompt is not None:
            send_one(self._pending_prompt)
        # Seed the transient marker to a client attaching while the override is live.
        if self._coordinator is not None and self._coordinator.unsandboxed:
            send_one({"type": "sandbox_marker",
                      "payload": {"unsandboxed": True}})
        self._note_first_leased()
        return snapshot

    def _on_detach_pulse(self) -> None:
        self._pulse_attach_change()
        self._maybe_pause_on_last_lease()

    def _note_first_leased(self) -> None:
        """Latch the first 0 -> 1 LEASED transition (via attach OR set-lease) and
        run the first-attach callback once. The zero-leased gates read this latch
        as 'a keep-alive client has ever attached'."""
        if self._socket.leased_count >= 1 and not self._first_leased_attach_fired:
            self._first_leased_attach_fired = True
            if self._on_first_attach is not None:
                self._on_first_attach()

    def _on_lease_change(self) -> None:
        self._pulse_attach_change()
        self._note_first_leased()
        self._maybe_pause_on_last_lease()

    def _maybe_pause_on_last_lease(self) -> None:
        if (self._coordinator is None
                or not self._first_leased_attach_fired
                or self._socket.leased_count != 0
                or self._pending_prompt is not None
                or self._in_wait_for_turn):
            return
        with contextlib.suppress(anyio.WouldBlock, anyio.ClosedResourceError,
                                 anyio.BrokenResourceError):
            self._control_send.send_nowait((_INTERNAL_PAUSE, None))

    def request_save(self, reason: str | None = None) -> None:
        self._enqueue_control(CONTROL_SAVE, reason)

    def _enqueue_control(self, action: str, reason: str | None = None) -> None:
        if action == CONTROL_ABANDON:
            # Terminal control: a guaranteed off-queue signal the consumer checks
            # every iteration, PLUS a best-effort enqueue so an empty/non-full
            # queue wakes the consumer immediately. When the queue is full the
            # enqueue is dropped but the buffered items still wake the loop, which
            # then fires abandon on its very next iteration via the signal.
            self._abandon_requested = True
            with contextlib.suppress(anyio.WouldBlock, anyio.ClosedResourceError):
                self._control_send.send_nowait((action, reason))
            return
        if action in _DISPOSITION_PRECEDENCE:
            # Guaranteed delivery like abandon: the request and its reason live
            # off-queue, so a full-queue drop loses neither.
            self._dispositions[action] = reason
            with contextlib.suppress(anyio.WouldBlock, anyio.ClosedResourceError):
                self._control_send.send_nowait((action, reason))
            return
        try:
            self._control_send.send_nowait((action, reason))
        except anyio.WouldBlock:
            _log.warning(
                "session control queue overflow; dropping ordinary control %r", action)
        except anyio.ClosedResourceError:
            pass   # the run is over; the end frame may still be draining
        else:
            if action == CONTROL_INTERRUPT_LEAVE:
                # On arrival, ahead of the unwind that clears _pending_prompt.
                self._latch_leave()

    async def _control_loop(self) -> None:
        async for action, reason in self._control_recv:
            if self._abandon_requested:
                await self._dispatch_control(CONTROL_ABANDON)
                return
            for disposition in _DISPOSITION_PRECEDENCE:
                if disposition in self._dispositions:
                    await self._dispatch_control(
                        disposition, self._dispositions[disposition])
                    return
            await self._dispatch_control(action, reason)

    async def _dispatch_control(self, action: str, reason: str | None = None) -> None:
        if action == _INTERNAL_PAUSE:
            if self._coordinator is not None and self.leased_count == 0:
                self._latch_leave()
                await self._coordinator.signal_pause(reason="detached", via="frame")
            return
        if action == CONTROL_INTERRUPT and self._coordinator is not None:
            await self._coordinator.signal_pause(reason="ctrl-c", via="frame")
        elif action == CONTROL_SET_MODEL and self._coordinator is not None:
            # Non-terminal: stash the arg, pause like ctrl-c; the hibernation gate's
            # side-effect wake arm unwinds the parked between-turns solicit
            # (attached + leased, so the unlease arm never fires) and the hold loop
            # applies the pending model on park.
            self._coordinator.set_pending_model(reason or "")
            await self._coordinator.signal_pause(
                reason="model-override", via="frame")
            self._pulse_side_effect()
        elif action == CONTROL_COMPACT and self._coordinator is not None:
            await self._coordinator.signal_pause(reason="compact", via="frame")
            self._pulse_side_effect()
        elif action == CONTROL_SET_SANDBOX and self._coordinator is not None:
            self._coordinator.set_unsandboxed(reason == "off")
            # Transient: broadcast-only via _emit, never the event log, so it can't replay stale.
            self._emit({"type": "sandbox_marker",
                        "payload": {"unsandboxed": reason == "off"}})
            self._emit({"type": "script_output",
                        "payload": {"stdout": SANDBOX_OFF_CONFIRMED if reason == "off"
                                    else SANDBOX_ON_FROM_OFF}})
            if self._pending_prompt is not None:
                if reason == "off":
                    # The user typed `off` because an agent asked them to: resume the
                    # parked solicit rather than making them type a throwaway reply.
                    self._socket.inject_input(ContinuationNote(text=SANDBOX_OFF_NOTE))
                else:
                    # Re-open the parked prompt so the client's _answer_gates loop,
                    # blocked on gate_recv after set_sandbox, receives a gate.
                    self._emit(self._pending_prompt)
        elif action == CONTROL_SET_PROVIDER and self._set_provider is not None:
            assert reason is not None
            self._set_provider(reason)
            # Transient: broadcast-only via _emit, never the event log, so a
            # later attach cannot replay a marker its reconcile already cleared.
            self._emit({"type": "provider_marker",
                        "payload": {"provider": reason}})
            if self._pending_prompt is not None:
                self._emit(self._pending_prompt)
        elif action == CONTROL_INTERRUPT_LEAVE and self._coordinator is not None:
            self._latch_leave()
            if reason is not None:
                self._coordinator.leave_reason = reason
            await self._coordinator.signal_pause(reason=reason or "ctrl-d", via="frame")
        elif action == CONTROL_INTERRUPT_FORCE and self._coordinator is not None:
            await self._coordinator.signal_force()
        elif action == CONTROL_RETRY_NOW and self._coordinator is not None:
            self._coordinator.request_retry_now()
        elif action == CONTROL_DETACH:
            await self._socket.detach_all()
        elif action in LEAVE_DISPOSITIONS:
            self.leave = action
            if action == CONTROL_SAVE:
                self.save_reason = reason
            if action == CONTROL_ABANDON and self._on_abandon is not None:
                self._on_abandon()
            if self._coordinator is not None and reason is not None:
                self._coordinator.leave_reason = reason
            if self._run_scope is None:
                raise RuntimeError(f"{action} dispatched outside serve()")
            self._run_scope.cancel()
