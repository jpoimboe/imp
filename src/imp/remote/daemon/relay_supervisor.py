"""RelaySupervisor — the router's read side: relay every resident session's curated
events to a sink."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio

from imp.remote.daemon.directory import resident_session_ids
from imp.remote.daemon.subscriptions import Subscriptions
from imp.remote.session.discover import session_socket_path
from imp.remote.session.relay import SessionRelay


class RelaySupervisor:
    def __init__(
        self,
        *,
        control_root: Path,
        sink: Callable[[dict[str, Any]], None],
        events: tuple[str, ...],
        title_for: Callable[[str], str],
        poll_gap: float = 0.5,
        subscriptions: Subscriptions | None = None,
    ) -> None:
        self._control_root = control_root
        self._sink = sink
        self._events = events
        self._title_for = title_for
        self._poll_gap = poll_gap
        self._subscriptions = subscriptions
        self._running: set[str] = set()
        self._gated: set[str] = set()

    async def run(self) -> None:
        """Poll the directory forever, running one relay per resident change. Cancel
        the surrounding scope to stop."""
        async with anyio.create_task_group() as tg:
            while True:
                resident = resident_session_ids(self._control_root)
                # A gated change that has since hibernated (left the directory) is
                # eligible again, so a reply that rehydrates it re-attaches a relay.
                self._gated &= resident
                for change_id in resident:
                    if change_id not in self._running and change_id not in self._gated:
                        self._running.add(change_id)
                        tg.start_soon(self._relay_one, change_id)
                await anyio.sleep(self._poll_gap)

    def _subscribed_probe(self, change_id: str) -> Callable[[], bool] | None:
        subs = self._subscriptions   # local so mypy narrows past the None-return
        if subs is None:
            return None
        return lambda: subs.contains(change_id)

    async def _relay_one(self, change_id: str) -> None:
        relay = SessionRelay(
            socket_path=str(session_socket_path(self._control_root, change_id)),
            change_id=change_id,
            title=self._title_for(change_id),
            events=self._events,
            sink=self._sink,
            is_subscribed=self._subscribed_probe(change_id),
        )
        try:
            await relay.run()
        except OSError:
            pass
        finally:
            # Latch and unregister run together with no await between them, so a
            # re-attach poll cannot slip in before the latch is set. A latch on a
            # socket-dropped (non-resident) change is harmless: run() prunes it.
            if relay.gated:
                self._gated.add(change_id)
            self._running.discard(change_id)
