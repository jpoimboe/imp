"""Transport-agnostic control plane: list / reply / create / abandon, routed by change_id."""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from imp.remote.daemon.discover import ChangeInfo
from imp.remote.daemon.focus import Focus


class ControlPlane:
    def __init__(
        self,
        *,
        deliver: Callable[[str, str], Awaitable[None]],
        create: Callable[[str], None],
        abandon: Callable[[str], int],
        list_changes: Callable[[], list[ChangeInfo]],
        focus: Focus,
        snapshot: Callable[[str], tuple[str, str, str] | None],
        recent_narrative: Callable[[str, int], list[str]],
        search: Callable[[str], list[tuple[str, str, str, str]]] | None = None,
    ) -> None:
        self._deliver = deliver
        self._create = create
        self._abandon = abandon
        self._list_fn = list_changes
        self.focus = focus
        self._snapshot = snapshot
        self._recent_narrative = recent_narrative
        self._search = search

    def snapshot(self, change_id: str) -> tuple[str, str, str] | None:
        return self._snapshot(change_id)

    def recent_narrative(self, change_id: str, n: int) -> list[str]:
        return self._recent_narrative(change_id, n)

    def search(self, query: str) -> list[tuple[str, str, str, str]]:
        assert self._search is not None  # run_daemon always wires it
        return self._search(query)

    async def reply(self, change_id: str, text: str) -> None:
        """Deliver a reply into the change's session daemon, rehydrating if hibernated."""
        await self._deliver(change_id, text)

    def create(self, prompt: str) -> None:
        self._create(prompt)

    def abandon(self, change_id: str) -> int:
        return self._abandon(change_id)

    def list_changes(self) -> list[ChangeInfo]:
        return self._list_fn()
