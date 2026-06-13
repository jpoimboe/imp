"""Transport-agnostic control plane: list / reply / create / abandon, routed by change_id."""
from __future__ import annotations

from collections.abc import Callable

from imp.remote.daemon.discover import ChangeInfo
from imp.remote.daemon.server import SouthHub


class ControlPlane:
    def __init__(
        self,
        *,
        hub: SouthHub,
        resume: Callable[[str, str], None],
        create: Callable[[str], None],
        abandon: Callable[[str], None],
        list_changes: Callable[[], list[ChangeInfo]],
    ) -> None:
        self._hub: SouthHub = hub
        self._resume = resume
        self._create = create
        self._abandon = abandon
        self._list_fn = list_changes

    async def reply(self, change_id: str, text: str) -> str:
        """Route a reply: live south connection returns "live"; paused Change returns "resumed"."""
        if await self._hub.send_reply(change_id, text):
            return "live"
        self._resume(change_id, text)
        return "resumed"

    def create(self, prompt: str) -> None:
        self._create(prompt)

    def abandon(self, change_id: str) -> None:
        self._abandon(change_id)

    def list_changes(self) -> list[ChangeInfo]:
        return self._list_fn()
