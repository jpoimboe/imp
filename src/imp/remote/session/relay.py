"""Router read-side relay: attach to a resident session, curate events, sink, detach at gate."""
from __future__ import annotations

import contextlib
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
        is_subscribed: Callable[[], bool] | None = None,
        lease_poll_gap: float = 0.5,
    ) -> None:
        self._socket_path = socket_path
        self._change_id = change_id
        self._title = title
        self._curator = EventCurator(id=change_id, title=title, events=events)
        self._sink = sink
        self._is_subscribed = is_subscribed
        self._lease_poll_gap = lease_poll_gap
        self._gate_send: MemoryObjectSendStream[None] | None = None
        self.gated: bool = False

    async def run(self) -> None:
        """Relay events to sink; detach at gate; lease iff subscribed; OSError if none resident."""
        probe = self._is_subscribed   # local so mypy narrows it for the closure/start_soon
        initial_lease = probe() if probe is not None else False
        client = SessionClient(
            self._socket_path, self._change_id, self._title,
            on_event=self._on_event, lease=initial_lease)
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
                if probe is not None:
                    tg.start_soon(self._poll_lease, client, probe, initial_lease)
                await client.receive_events()
                tg.cancel_scope.cancel()
        finally:
            self._gate_send = None
            await gate_send.aclose()
            await gate_recv.aclose()
            await client.aclose()

    async def _poll_lease(
        self, client: SessionClient, probe: Callable[[], bool], current: bool,
    ) -> None:
        while True:
            await anyio.sleep(self._lease_poll_gap)
            desired = probe()
            if desired != current:
                current = desired
                with contextlib.suppress(
                    RuntimeError, anyio.BrokenResourceError, anyio.ClosedResourceError,
                ):
                    await client.send_set_lease(desired)

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
