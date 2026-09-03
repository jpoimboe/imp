# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Session keystroke mapper: forwards interrupt, abandon, and detach gestures as control frames."""

from __future__ import annotations

from imp.cli.signals import InterruptCoordinator
from imp.remote.session.client import SessionClient
from imp.remote.session.protocol import (
    CONTROL_ABANDON,
    CONTROL_COMPACT,
    CONTROL_INTERRUPT,
    CONTROL_INTERRUPT_FORCE,
    CONTROL_RETRY_NOW,
    CONTROL_SET_MODEL,
    CONTROL_SET_SANDBOX,
)


class SessionControls:
    def __init__(self, client: SessionClient, coord: InterruptCoordinator) -> None:
        self._client = client
        coord.attach_runner(self._forward_interrupt)
        coord.set_force_forward_hook(self._forward_interrupt_force)
        coord.set_retry_now_hook(self._forward_retry_now)

    async def _forward_interrupt(self) -> None:
        await self._client.send_control(CONTROL_INTERRUPT)

    async def _forward_interrupt_force(self) -> None:
        await self._client.send_control(CONTROL_INTERRUPT_FORCE)

    async def _forward_retry_now(self) -> None:
        await self._client.send_control(CONTROL_RETRY_NOW)

    async def abandon(self) -> None:
        await self._client.send_control(CONTROL_ABANDON)

    async def compact(self) -> None:
        await self._client.send_control(CONTROL_COMPACT)

    async def set_model(self, arg: str) -> None:
        await self._client.send_control(CONTROL_SET_MODEL, arg)

    async def set_sandbox(self, arg: str) -> None:
        await self._client.send_control(CONTROL_SET_SANDBOX, arg)

    async def detach(self) -> None:
        await self._client.aclose()
