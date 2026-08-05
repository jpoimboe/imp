"""Transport-agnostic control plane: list / reply / create, routed by quest_id."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from imp.remote.daemon.discover import QuestInfo
from imp.remote.daemon.subscriptions import Subscriptions
from imp.remote.session.reply import DeliveryOutcome


class ControlPlane:
    def __init__(
        self,
        *,
        deliver: Callable[[str, str], Awaitable[DeliveryOutcome]],
        create: Callable[[str], str],
        list_quests: Callable[[], list[QuestInfo]],
        subscriptions: Subscriptions,
        snapshot: Callable[[str], tuple[str, str, str] | None],
        recent_narrative: Callable[[str, int], list[dict[str, Any]]],
        resume: Callable[[str], str] | None = None,
        full_history: Callable[[str], list[dict[str, Any]]] | None = None,
        resident_ids: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        self._deliver = deliver
        self._create = create
        self._list_fn = list_quests
        self._resident_ids = resident_ids
        self.subscriptions = subscriptions
        self._snapshot = snapshot
        self._recent_narrative = recent_narrative
        self._resume = resume
        self._full_history = full_history

    def snapshot(self, quest_id: str) -> tuple[str, str, str] | None:
        return self._snapshot(quest_id)

    def recent_narrative(self, quest_id: str, n: int) -> list[dict[str, Any]]:
        return self._recent_narrative(quest_id, n)

    def resume(self, token: str) -> str:
        assert self._resume is not None  # run_daemon always wires it
        return self._resume(token)

    def full_history(self, quest_id: str) -> list[dict[str, Any]]:
        assert self._full_history is not None  # run_daemon always wires it
        return self._full_history(quest_id)

    async def reply(self, quest_id: str, text: str) -> DeliveryOutcome:
        """Deliver a reply into the quest's session daemon, rehydrating if hibernated."""
        return await self._deliver(quest_id, text)

    def create(self, prompt: str) -> str:
        return self._create(prompt)

    def list_quests(self) -> list[QuestInfo]:
        return self._list_fn()

    def resident_ids(self) -> frozenset[str]:
        assert self._resident_ids is not None  # run_daemon always wires it
        return self._resident_ids()
