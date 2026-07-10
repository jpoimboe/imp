"""Ephemeral alert-all mode: deliver buzz alerts for every Change while the
daemon runs (no persistence across restarts).

Transport-agnostic. Owned by run_daemon; read in the sender thread
(enabled/seen/mark) and written by the async command dispatch (set). One internal
lock guards both the mode flag and the delivery log."""
from __future__ import annotations

import threading


class AlertAll:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled = True
        self._delivered: dict[str, set[str]] = {}

    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set(self, on: bool) -> None:
        """Turn the mode on or off. Turning it off also clears the delivery log so
        a later re-enable starts clean and the log never grows unbounded."""
        with self._lock:
            self._enabled = on
            if not on:
                self._delivered.clear()

    def seen(self, quest_id: str, event_id: str) -> bool:
        with self._lock:
            ids = self._delivered.get(quest_id)
            return ids is not None and event_id in ids

    def mark(self, quest_id: str, event_id: str) -> None:
        with self._lock:
            self._delivered.setdefault(quest_id, set()).add(event_id)
