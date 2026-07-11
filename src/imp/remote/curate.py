"""Curate raw engine events: filter by allowlist, track step/status, augment with change context."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


class EventCurator:
    def __init__(
        self, *, id: str, title_provider: Callable[[], str], events: tuple[str, ...],
    ) -> None:
        self._id = id
        self._title_provider = title_provider
        self._title = ""
        self._events = events
        self._step: str | None = None
        self._status = "running"

    def curate(self, event: Any) -> dict[str, Any] | None:
        """Return augmented event for allowed types (tracking precedes filter), or None to drop."""
        if not isinstance(event, dict):
            return None
        etype = event.get("type")
        self._track(event)
        if not self._allowed(etype):
            return None
        # Re-read while empty to catch a mid-session rename; freeze on first non-empty.
        if not self._title:
            self._title = self._title_provider()
        # Quest context is namespaced under "change" so it never clobbers the
        # event's OWN id/payload fields (events carry their own log id).
        return {
            **event,
            "change": {
                "id": self._id, "title": self._title,
                "step": self._step, "status": self._status,
            },
        }

    def _allowed(self, etype: Any) -> bool:
        return "all" in self._events or etype in self._events

    def _track(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        payload = event.get("payload") or {}
        if etype == "step_started" and payload.get("step") is not None:
            self._step = payload["step"]
        elif etype == "workflow_paused":
            self._status = "paused"
        elif etype == "merge_blocked":
            self._status = "merge_blocked"
        elif etype == "change_merged":
            self._status = "completed"
