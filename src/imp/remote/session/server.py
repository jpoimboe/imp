from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from imp.remote.session.protocol import (
    CONTROL_ABANDON,
    CONTROL_ARCHIVE,
    CONTROL_DETACH,
    CONTROL_DISCARD,
    CONTROL_INTERRUPT,
    CONTROL_INTERRUPT_LEAVE,
    CONTROL_SAVE,
)
from imp.remote.session.socket_server import ReplaySnapshot, SessionSocket

_log = logging.getLogger("imp.remote.session.server")

RunFn = Callable[..., Awaitable[Any]]

_CONTROL_QUEUE_DEPTH = 64
_INTERNAL_PAUSE = "__detach_pause__"  # server-internal: never a client frame action

# Startup grace: how long a hibernation gate waits for the FIRST client to attach
# before falling back to hibernation. The spawning client (interactive or remote
# reply delivery) attaches a beat after the daemon starts resuming, so a
# bare-turn-end solicit can reach the gate first; without this wait it would
# hibernate on the transient zero-client window and the resume would pause+exit.
# Generous vs. attach latency (which loses by ~tens of ms); only a crashed client
# ever exhausts it.
_DEFAULT_FIRST_ATTACH_TIMEOUT = 30.0


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
    ) -> None:
        self._coordinator = coordinator
        self._on_abandon = on_abandon
        self._lock = lock
        self._hibernate = hibernate
        self._events_path = events_path
        self._on_first_attach = on_first_attach
        self._first_attach_timeout = first_attach_timeout
        # Set on the first 0 -> 1 LEASED attach and never cleared: distinguishes
        # "no keep-alive client has arrived yet" (startup — wait) from "a
        # keep-alive client attached and left" (hibernate now) at the
        # zero-leased gates.
        self._first_leased_attach_fired: bool = False
        self.abandoned = False
        self.saved = False
        self.archived = False
        self.discarded = False
        self._abandon_requested = False
        self._save_requested = False
        self._archive_requested = False
        self._discard_requested = False
        self._serve_scope: anyio.CancelScope | None = None
        self._pending_prompt: dict[str, Any] | None = None
        self._last_gate_event: dict[str, Any] | None = None
        self._in_wait_for_turn = False
        # Initialized in open() because anyio.Event needs a running event loop.
        self._attach_changed: anyio.Event | None = None
        self._socket = SessionSocket(
            socket_path,
            on_attach=self._on_attach_pulse,
            on_detach=self._on_detach_pulse,
            on_control=self._enqueue_control,
            on_lease_change=self._on_lease_change,
        )
        ctl_send, ctl_recv = anyio.create_memory_object_stream[tuple[str, str | None]](
            max_buffer_size=_CONTROL_QUEUE_DEPTH)
        self._control_send: MemoryObjectSendStream[tuple[str, str | None]] = ctl_send
        self._control_recv: MemoryObjectReceiveStream[tuple[str, str | None]] = ctl_recv

    @property
    def attached_count(self) -> int:
        return self._socket.attached_count

    @property
    def leased_count(self) -> int:
        return self._socket.leased_count

    async def open(self) -> None:
        self._attach_changed = anyio.Event()
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
                self._serve_scope = tg.cancel_scope
                tg.start_soon(self._socket.serve)
                async with anyio.create_task_group() as pump_tg:
                    pump_tg.start_soon(self._control_loop)
                    try:
                        result = await run_fn(
                            on_event=self._emit, user_chat_input_fn=self._chat)
                    finally:
                        await self._control_send.aclose()
                tg.cancel_scope.cancel()
        finally:
            await self._socket.close()
        return result

    def _emit(self, event: dict[str, Any]) -> None:
        if event.get("type") == "awaiting_user_input":
            self._last_gate_event = event
        self._socket.broadcast(event)

    async def _chat(self, _query: str) -> str:
        self.set_pending_prompt(self._last_gate_event)
        try:
            text = (await self._hibernation_gate() if self._hibernate
                    else await self._socket.next_input())
            self._emit({"type": "user_input_resolved", "payload": {}})
            return text
        finally:
            self.set_pending_prompt(None)
            self._last_gate_event = None

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

    async def _hibernation_gate(self) -> str:
        from imp.engine.awaiting_user_input import AwaitingUserInput

        holder: dict[str, str] = {}
        async with anyio.create_task_group() as tg:
            async def _await_input() -> None:
                holder["text"] = await self._socket.next_input()
                tg.cancel_scope.cancel()

            async def _await_hibernate() -> None:
                await self._wait_until_unleased()
                tg.cancel_scope.cancel()

            tg.start_soon(_await_input)
            tg.start_soon(_await_hibernate)
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

    def set_pending_prompt(self, event: dict[str, Any] | None) -> None:
        """Track a prompt event to re-emit to clients that connect after it fires."""
        self._pending_prompt = event

    def _on_attach_pulse(
        self, _attach: Any, send_one: Callable[[dict[str, Any]], None],
    ) -> ReplaySnapshot | None:
        self._pulse_attach_change()
        snapshot = (
            ReplaySnapshot.capture(self._events_path)
            if self._events_path is not None else None)
        if self._pending_prompt is not None:
            send_one(self._pending_prompt)
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

    def _enqueue_control(self, action: str, reason: str | None = None) -> None:
        if action == CONTROL_ABANDON:
            # Terminal control: a guaranteed off-queue signal the consumer checks
            # every iteration, PLUS a best-effort enqueue so an empty/non-full
            # queue wakes the consumer immediately. When the queue is full the
            # enqueue is dropped but the buffered items still wake the loop, which
            # then fires abandon on its very next iteration via the signal.
            self._abandon_requested = True
            with contextlib.suppress(anyio.WouldBlock):
                self._control_send.send_nowait((action, reason))
            return
        if action == CONTROL_SAVE:
            # Same guaranteed-delivery pattern as abandon: flag is always set;
            # enqueue is best-effort so the loop wakes immediately when possible.
            self._save_requested = True
            with contextlib.suppress(anyio.WouldBlock):
                self._control_send.send_nowait((action, reason))
            return
        if action == CONTROL_ARCHIVE:
            self._archive_requested = True
            with contextlib.suppress(anyio.WouldBlock):
                self._control_send.send_nowait((action, reason))
            return
        if action == CONTROL_DISCARD:
            self._discard_requested = True
            with contextlib.suppress(anyio.WouldBlock):
                self._control_send.send_nowait((action, reason))
            return
        try:
            self._control_send.send_nowait((action, reason))
        except anyio.WouldBlock:
            _log.warning(
                "session control queue overflow; dropping ordinary control %r", action)

    async def _control_loop(self) -> None:
        async for action, reason in self._control_recv:
            if self._abandon_requested:
                await self._dispatch_control(CONTROL_ABANDON)
                return
            if self._save_requested:
                await self._dispatch_control(CONTROL_SAVE)
                return
            if self._archive_requested:
                await self._dispatch_control(CONTROL_ARCHIVE)
                return
            if self._discard_requested:
                await self._dispatch_control(CONTROL_DISCARD)
                return
            await self._dispatch_control(action, reason)

    async def _dispatch_control(self, action: str, reason: str | None = None) -> None:
        if action == _INTERNAL_PAUSE:
            if self._coordinator is not None and self.leased_count == 0:
                await self._coordinator.signal_pause(reason="detached")
            return
        if action == CONTROL_INTERRUPT and self._coordinator is not None:
            await self._coordinator.signal_pause(reason="ctrl-c")
        elif action == CONTROL_INTERRUPT_LEAVE and self._coordinator is not None:
            await self._coordinator.signal_pause(reason=reason or "ctrl-d")
        elif action == CONTROL_DETACH:
            await self._socket.detach_all()
        elif action == CONTROL_ABANDON:
            self.abandoned = True
            if self._on_abandon is not None:
                self._on_abandon()
            if self._serve_scope is None:
                raise RuntimeError("abandon dispatched outside serve()")
            self._serve_scope.cancel()
        elif action == CONTROL_SAVE:
            self.saved = True
            if self._serve_scope is None:
                raise RuntimeError("save dispatched outside serve()")
            self._serve_scope.cancel()
        elif action == CONTROL_ARCHIVE:
            self.archived = True
            if self._serve_scope is None:
                raise RuntimeError("archive dispatched outside serve()")
            self._serve_scope.cancel()
        elif action == CONTROL_DISCARD:
            self.discarded = True
            if self._serve_scope is None:
                raise RuntimeError("discard dispatched outside serve()")
            self._serve_scope.cancel()
