"""Router read-side relay: attach to a resident session, curate events, sink, detach at gate."""
from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import anyio
from anyio.streams.memory import MemoryObjectSendStream

from imp.remote.curate import EventCurator
from imp.remote.session.client import SessionClient


class SessionRelay:
    def __init__(
        self,
        *,
        socket_path: str,
        change_id: str,
        title: str,
        events: tuple[str, ...],
        sink: Callable[[dict[str, Any]], None],
    ) -> None:
        self._socket_path = socket_path
        self._change_id = change_id
        self._title = title
        self._curator = EventCurator(id=change_id, title=title, events=events)
        self._sink = sink
        self._gate_send: MemoryObjectSendStream[None] | None = None
        self.gated: bool = False

    async def run(self) -> None:
        """Relay curated events to sink; detach at gate. Raises OSError if none is resident."""
        client = SessionClient(
            self._socket_path, self._change_id, self._title,
            on_event=self._on_event)
        await client.connect()
        gate_send, gate_recv = anyio.create_memory_object_stream[None](
            max_buffer_size=math.inf)
        self._gate_send = gate_send
        try:
            async with anyio.create_task_group() as tg:
                async def _detach_at_gate() -> None:
                    await gate_recv.receive()
                    tg.cancel_scope.cancel()

                tg.start_soon(_detach_at_gate)
                await client.receive_events()
                tg.cancel_scope.cancel()
        finally:
            self._gate_send = None
            await gate_send.aclose()
            await gate_recv.aclose()
            await client.aclose()

    def _on_event(self, event: dict[str, Any]) -> None:
        curated = self._curator.curate(event)
        # sink-forward before gate signal: gate event must reach sink before detach
        if curated is not None:
            self._sink(curated)
        if (event.get("type") == "awaiting_user_input"
                and not event.get("_replay")
                and self._gate_send is not None):
            self.gated = True
            self._gate_send.send_nowait(None)
