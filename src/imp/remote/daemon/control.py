"""Transport-agnostic control plane: list / reply / create / abandon, routed by change_id."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from imp.remote.daemon.discover import ChangeInfo
from imp.remote.daemon.subscriptions import Subscriptions


class ControlPlane:
    def __init__(
        self,
        *,
        deliver: Callable[[str, str], Awaitable[None]],
        create: Callable[[str], None],
        abandon: Callable[[str], int],
        list_changes: Callable[[], list[ChangeInfo]],
        subscriptions: Subscriptions,
        snapshot: Callable[[str], tuple[str, str, str] | None],
        recent_narrative: Callable[[str, int], list[dict[str, Any]]],
        search: Callable[[str], list[tuple[str, str, str, str]]] | None = None,
        resume: Callable[[str], str] | None = None,
        save: Callable[[str], None] | None = None,
        full_history: Callable[[str], list[dict[str, Any]]] | None = None,
        resident_ids: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        self._deliver = deliver
        self._create = create
        self._abandon = abandon
        self._list_fn = list_changes
        self._resident_ids = resident_ids
        self.subscriptions = subscriptions
        self._snapshot = snapshot
        self._recent_narrative = recent_narrative
        self._search = search
        self._resume = resume
        self._save = save
        self._full_history = full_history

    def snapshot(self, change_id: str) -> tuple[str, str, str] | None:
        return self._snapshot(change_id)

    def recent_narrative(self, change_id: str, n: int) -> list[dict[str, Any]]:
        return self._recent_narrative(change_id, n)

    def search(self, query: str) -> list[tuple[str, str, str, str]]:
        assert self._search is not None  # run_daemon always wires it
        return self._search(query)

    def resume(self, token: str) -> str:
        assert self._resume is not None  # run_daemon always wires it
        return self._resume(token)

    def save(self, change_id: str) -> None:
        assert self._save is not None  # run_daemon always wires it
        self._save(change_id)

    def full_history(self, change_id: str) -> list[dict[str, Any]]:
        assert self._full_history is not None  # run_daemon always wires it
        return self._full_history(change_id)

    async def reply(self, change_id: str, text: str) -> None:
        """Deliver a reply into the change's session daemon, rehydrating if hibernated."""
        await self._deliver(change_id, text)

    def create(self, prompt: str) -> None:
        self._create(prompt)

    def abandon(self, change_id: str) -> int:
        return self._abandon(change_id)

    def list_changes(self) -> list[ChangeInfo]:
        return self._list_fn()

    def resident_ids(self) -> frozenset[str]:
        assert self._resident_ids is not None  # run_daemon always wires it
        return self._resident_ids()
