"""Router read-side relay: attach a resident session, curate+sink events; awaiting rides lease."""
from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

import anyio

from imp.remote.curate import EventCurator
from imp.remote.session.client import SessionClient


class SessionRelay:
    def __init__(
        self,
        *,
        socket_path: str,
        change_id: str,
        title_provider: Callable[[], str],
        events: tuple[str, ...],
        sink: Callable[[dict[str, Any]], None],
        is_subscribed: Callable[[], bool] | None = None,
        lease_poll_gap: float = 0.5,
    ) -> None:
        self._socket_path = socket_path
        self._change_id = change_id
        # Hello is a one-shot attach frame: resolve once here; curator keeps the live provider.
        self._title = title_provider()
        self._curator = EventCurator(
            id=change_id, title_provider=title_provider, events=events)
        self._sink = sink
        self._is_subscribed = is_subscribed
        self._lease_poll_gap = lease_poll_gap
        self._awaiting: bool = False

    async def run(self) -> None:
        """Relay events; lease iff subscribed and not awaiting; OSError if no resident session."""
        probe = self._is_subscribed   # local so mypy narrows it for the closure/start_soon
        initial_lease = probe() if probe is not None else False
        client = SessionClient(
            self._socket_path, self._change_id, self._title,
            on_event=self._on_event, lease=initial_lease)
        await client.connect()
        try:
            async with anyio.create_task_group() as tg:
                if probe is not None:
                    tg.start_soon(self._poll_lease, client, probe, initial_lease)
                await client.receive_events()
                tg.cancel_scope.cancel()
        finally:
            await client.aclose()

    async def _poll_lease(
        self, client: SessionClient, probe: Callable[[], bool], current: bool,
    ) -> None:
        while True:
            await anyio.sleep(self._lease_poll_gap)
            desired = probe() and not self._awaiting
            if desired != current:
                current = desired
                with contextlib.suppress(
                    RuntimeError, anyio.BrokenResourceError, anyio.ClosedResourceError,
                ):
                    await client.send_set_lease(desired)

    def _on_event(self, event: dict[str, Any]) -> None:
        curated = self._curator.curate(event)
        if curated is not None:
            self._sink(curated)
        # A live (non-replay) event moves awaiting-ness: a gate sets it, anything
        # else clears it. Replayed frames never touch _awaiting.
        if not event.get("_replay"):
            self._awaiting = event.get("type") == "awaiting_user_input"
