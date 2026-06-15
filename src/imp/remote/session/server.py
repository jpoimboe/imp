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
    ) -> None:
        self._coordinator = coordinator
        self._on_abandon = on_abandon
        self._socket = SessionSocket(socket_path, on_control=self._enqueue_control)
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
        await self._socket.open()

    async def serve(self, run_fn: RunFn) -> Any:
        async with anyio.create_task_group() as tg:
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
        await self._socket.close()
        return result

    def _emit(self, event: dict[str, Any]) -> None:
        self._event_send.send_nowait(event)

    async def _pump(self) -> None:
        async for event in self._event_recv:
            await self._socket.broadcast(event)

    def _chat(self, _query: str) -> Awaitable[str]:
        return self._socket.next_input()

    def _enqueue_control(self, action: str) -> None:
        with contextlib.suppress(anyio.WouldBlock):
            self._control_send.send_nowait(action)

    async def _control_loop(self) -> None:
        async for action in self._control_recv:
            await self._dispatch_control(action)

    async def _dispatch_control(self, action: str) -> None:
        if action == CONTROL_INTERRUPT and self._coordinator is not None:
            await self._coordinator.signal_pause(reason="ctrl-c")
        elif action == CONTROL_ABANDON and self._on_abandon is not None:
            self._on_abandon()
