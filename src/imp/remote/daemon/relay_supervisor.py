"""RelaySupervisor — the router's read side: relay every resident session's curated
events to a sink."""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio

from imp.remote.daemon.directory import resident_session_ids
from imp.remote.daemon.subscriptions import Subscriptions
from imp.remote.session.discover import session_socket_path
from imp.remote.session.relay import SessionRelay

_log = logging.getLogger("imp.daemon")


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

    async def run(self) -> None:
        """Poll the directory forever, running one relay per resident change. Cancel
        the surrounding scope to stop."""
        failing = False
        async with anyio.create_task_group() as tg:
            while True:
                try:
                    resident = resident_session_ids(self._control_root)
                    for quest_id in resident:
                        if quest_id not in self._running:
                            self._running.add(quest_id)
                            tg.start_soon(self._relay_one, quest_id)
                    failing = False
                except Exception:
                    # A poll must never kill the daemon. Directory reads are guarded
                    # per-change in discover.py; this is the backstop for anything
                    # else (a probe error, a bug). Log the first failure of a run,
                    # suppress the repeats, and retry on the next poll. CancelledError
                    # is a BaseException, so cancel-to-stop still propagates.
                    if not failing:
                        failing = True
                        _log.warning("relay supervisor poll failed; retrying",
                                     exc_info=True)
                await anyio.sleep(self._poll_gap)

    def _subscribed_probe(self, quest_id: str) -> Callable[[], bool] | None:
        subs = self._subscriptions   # local so mypy narrows past the None-return
        if subs is None:
            return None
        return lambda: subs.contains(quest_id)

    async def _relay_one(self, quest_id: str) -> None:
        relay = SessionRelay(
            socket_path=str(session_socket_path(self._control_root, quest_id)),
            quest_id=quest_id,
            title_provider=lambda: self._title_for(quest_id),
            events=self._events,
            sink=self._sink,
            is_subscribed=self._subscribed_probe(quest_id),
        )
        try:
            await relay.run()
        except OSError:
            pass
        finally:
            self._running.discard(quest_id)
