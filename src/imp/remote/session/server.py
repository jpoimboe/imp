from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from anyio.streams.memory import (
    MemoryObjectReceiveStream,
    MemoryObjectSendStream,
)

from imp.remote.session.socket_server import SessionSocket

RunFn = Callable[..., Awaitable[Any]]


class SessionServer:
    def __init__(self, socket_path: str) -> None:
        self._socket = SessionSocket(socket_path)
        ev_send, ev_recv = anyio.create_memory_object_stream[dict[str, Any]](
            max_buffer_size=math.inf)
        self._event_send: MemoryObjectSendStream[dict[str, Any]] = ev_send
        self._event_recv: MemoryObjectReceiveStream[dict[str, Any]] = ev_recv

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
                try:
                    result = await run_fn(
                        on_event=self._emit, user_chat_input_fn=self._chat)
                finally:
                    await self._event_send.aclose()
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
