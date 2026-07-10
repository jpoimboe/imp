"""Per-run subscribed-Quest set + per-key replayed-event-id dedup; in-memory, no persistence."""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from typing import Any


class Subscriptions:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, set[str]] = {}

    def add(self, quest_id: str, replayed_ids: Iterable[str] = ()) -> None:
        with self._lock:
            self._subs[quest_id] = set(replayed_ids)

    def remove(self, quest_id: str) -> None:
        with self._lock:
            self._subs.pop(quest_id, None)

    def contains(self, quest_id: str) -> bool:
        with self._lock:
            return quest_id in self._subs

    def all(self) -> list[str]:
        with self._lock:
            return list(self._subs)

    def seen(self, quest_id: str, event_id: str) -> bool:
        with self._lock:
            ids = self._subs.get(quest_id)
            return ids is not None and event_id in ids

    def mark_seen(self, quest_id: str, event_id: str) -> None:
        with self._lock:
            ids = self._subs.get(quest_id)
            if ids is not None:
                ids.add(event_id)

    def snapshot_and_add(
        self, quest_id: str, snapshot: Callable[[], list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Snapshot history + register membership/replayed-ids atomically under the lock."""
        with self._lock:
            events = snapshot()
            self._subs[quest_id] = {e["id"] for e in events}
            return events
