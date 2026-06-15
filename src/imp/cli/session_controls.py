"""Session keystroke mapper: forwards interrupt, abandon, and detach gestures as control frames."""

from __future__ import annotations

from imp.cli.signals import InterruptCoordinator
from imp.remote.session.client import SessionClient
from imp.remote.session.protocol import CONTROL_ABANDON, CONTROL_INTERRUPT


class SessionControls:
    def __init__(self, client: SessionClient, coord: InterruptCoordinator) -> None:
        self._client = client
        coord.attach_runner(self._forward_interrupt)

    async def _forward_interrupt(self) -> None:
        await self._client.send_control(CONTROL_INTERRUPT)

    async def abandon(self) -> None:
        await self._client.send_control(CONTROL_ABANDON)

    async def detach(self) -> None:
        await self._client.aclose()
