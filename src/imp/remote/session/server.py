from __future__ import annotations

import contextlib
import math
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from anyio.streams.memory import (
    MemoryObjectReceiveStream,
    MemoryObjectSendStream,
)

from imp.remote.session.protocol import CONTROL_ABANDON, CONTROL_INTERRUPT
from imp.remote.session.socket_server import SessionSocket

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
    ) -> None:
        self._coordinator = coordinator
        self._on_abandon = on_abandon
        self._lock = lock
        self._hibernate = hibernate
        self.abandoned = False
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
        ev_send, ev_recv = anyio.create_memory_object_stream[dict[str, Any]](
            max_buffer_size=math.inf)
        self._event_send: MemoryObjectSendStream[dict[str, Any]] = ev_send
        self._event_recv: MemoryObjectReceiveStream[dict[str, Any]] = ev_recv
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
                    pump_tg.start_soon(self._pump)
                    pump_tg.start_soon(self._control_loop)
                    try:
                        result = await run_fn(
                            on_event=self._emit, user_chat_input_fn=self._chat)
                    finally:
                        await self._event_send.aclose()
                        await self._control_send.aclose()
                tg.cancel_scope.cancel()
        finally:
            await self._socket.close()
        return result

    def _emit(self, event: dict[str, Any]) -> None:
        self._event_send.send_nowait(event)

    async def _pump(self) -> None:
        async for event in self._event_recv:
            await self._socket.broadcast(event)

    def _chat(self, _query: str) -> Awaitable[str]:
        if self._hibernate:
            return self._hibernation_gate()
        return self._socket.next_input()

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

    def set_pending_prompt(self, event: dict[str, Any] | None) -> None:
        """Track a prompt event to re-emit to clients that connect after it fires."""
        self._pending_prompt = event

    def _on_attach_pulse(self, _attach: Any) -> None:
        self._pulse_attach_change()
        if self._pending_prompt is not None:
            self._emit(self._pending_prompt)

    def _on_detach_pulse(self) -> None:
        self._pulse_attach_change()

    def _enqueue_control(self, action: str) -> None:
        with contextlib.suppress(anyio.WouldBlock):
            self._control_send.send_nowait(action)

    async def _control_loop(self) -> None:
        async for action in self._control_recv:
            await self._dispatch_control(action)

    async def _dispatch_control(self, action: str) -> None:
        if action == CONTROL_INTERRUPT and self._coordinator is not None:
            await self._coordinator.signal_pause(reason="ctrl-c")
        elif action == CONTROL_ABANDON:
            self.abandoned = True
            if self._on_abandon is not None:
                self._on_abandon()
            if self._serve_scope is None:
                raise RuntimeError("abandon dispatched outside serve()")
            self._serve_scope.cancel()
