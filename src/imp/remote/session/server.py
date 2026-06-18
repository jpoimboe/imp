from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from imp.remote.session.protocol import CONTROL_ABANDON, CONTROL_DETACH, CONTROL_INTERRUPT
from imp.remote.session.socket_server import ReplaySnapshot, SessionSocket

_log = logging.getLogger("imp.remote.session.server")

RunFn = Callable[..., Awaitable[Any]]

_CONTROL_QUEUE_DEPTH = 64


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
    ) -> None:
        self._coordinator = coordinator
        self._on_abandon = on_abandon
        self._lock = lock
        self._hibernate = hibernate
        self._events_path = events_path
        self.abandoned = False
        self._abandon_requested = False
        self._serve_scope: anyio.CancelScope | None = None
        self._pending_prompt: dict[str, Any] | None = None
        # Initialized in open() because anyio.Event needs a running event loop.
        self._attach_changed: anyio.Event | None = None
        self._socket = SessionSocket(
            socket_path,
            on_attach=self._on_attach_pulse,
            on_detach=self._on_detach_pulse,
            on_control=self._enqueue_control,
        )
        ctl_send, ctl_recv = anyio.create_memory_object_stream[str](
            max_buffer_size=_CONTROL_QUEUE_DEPTH)
        self._control_send: MemoryObjectSendStream[str] = ctl_send
        self._control_recv: MemoryObjectReceiveStream[str] = ctl_recv

    @property
    def attached_count(self) -> int:
        return self._socket.attached_count

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
            self.set_pending_prompt(event)
        self._socket.broadcast(event)

    async def _chat(self, _query: str) -> str:
        try:
            if self._hibernate:
                return await self._hibernation_gate()
            return await self._socket.next_input()
        finally:
            self.set_pending_prompt(None)

    async def _hibernation_gate(self) -> str:
        from imp.agents.tools.wait_for_user import AwaitingUserInput

        if self._socket.attached_count == 0:
            raise AwaitingUserInput()
        holder: dict[str, str] = {}
        async with anyio.create_task_group() as tg:
            async def _await_input() -> None:
                holder["text"] = await self._socket.next_input()
                tg.cancel_scope.cancel()

            async def _await_all_detached() -> None:
                # Condition loop: capture event, test condition, await — so a detach
                # that fires between test and await still wakes us (no lost wakeup),
                # and a re-attach that replaces the event is re-read on next iteration.
                while True:
                    evt = self._attach_changed
                    if self._socket.attached_count == 0:
                        break
                    assert evt is not None
                    await evt.wait()
                tg.cancel_scope.cancel()

            tg.start_soon(_await_input)
            tg.start_soon(_await_all_detached)
        if "text" not in holder:
            raise AwaitingUserInput()
        return holder["text"]

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
        from imp.agents.tools.wait_for_user import AwaitingUserInput
        while True:
            if predicate():
                return
            if self._socket.attached_count == 0:
                raise AwaitingUserInput()
            with anyio.move_on_after(poll_interval):
                await self.wait_for_attach_change()

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
        return snapshot

    def _on_detach_pulse(self) -> None:
        self._pulse_attach_change()

    def _enqueue_control(self, action: str) -> None:
        if action == CONTROL_ABANDON:
            # Terminal control: a guaranteed off-queue signal the consumer checks
            # every iteration, PLUS a best-effort enqueue so an empty/non-full
            # queue wakes the consumer immediately. When the queue is full the
            # enqueue is dropped but the buffered items still wake the loop, which
            # then fires abandon on its very next iteration via the signal.
            self._abandon_requested = True
            with contextlib.suppress(anyio.WouldBlock):
                self._control_send.send_nowait(action)
            return
        try:
            self._control_send.send_nowait(action)
        except anyio.WouldBlock:
            _log.warning(
                "session control queue overflow; dropping ordinary control %r", action)

    async def _control_loop(self) -> None:
        async for action in self._control_recv:
            if self._abandon_requested:
                await self._dispatch_control(CONTROL_ABANDON)
                return
            await self._dispatch_control(action)

    async def _dispatch_control(self, action: str) -> None:
        if action == CONTROL_INTERRUPT and self._coordinator is not None:
            await self._coordinator.signal_pause(reason="ctrl-c")
        elif action == CONTROL_DETACH:
            await self._socket.detach_all()
        elif action == CONTROL_ABANDON:
            self.abandoned = True
            if self._on_abandon is not None:
                self._on_abandon()
            if self._serve_scope is None:
                raise RuntimeError("abandon dispatched outside serve()")
            self._serve_scope.cancel()
