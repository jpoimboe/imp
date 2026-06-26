"""Per-run subscribed-Change set + per-key replayed-event-id dedup; in-memory, no persistence."""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from typing import Any


class Subscriptions:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, set[str]] = {}

    def add(self, change_id: str, replayed_ids: Iterable[str] = ()) -> None:
        with self._lock:
            self._subs[change_id] = set(replayed_ids)

    def remove(self, change_id: str) -> None:
        with self._lock:
            self._subs.pop(change_id, None)

    def contains(self, change_id: str) -> bool:
        with self._lock:
            return change_id in self._subs

    def all(self) -> list[str]:
        with self._lock:
            return list(self._subs)

    def seen(self, change_id: str, event_id: str) -> bool:
        with self._lock:
            ids = self._subs.get(change_id)
            return ids is not None and event_id in ids

    def snapshot_and_add(
        self, change_id: str, snapshot: Callable[[], list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Snapshot history + register membership/replayed-ids atomically under the lock."""
        with self._lock:
            events = snapshot()
            self._subs[change_id] = {e["id"] for e in events}
            return events
