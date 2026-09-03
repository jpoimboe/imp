# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Deliver one reply into a quest's session daemon (attach + inject + detach)."""
from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

import anyio

from imp.engine.quest import Quest
from imp.remote.session.attach import build_session_attach
from imp.remote.session.client import SessionClient

AttachFn = Callable[[Callable[[dict[str, Any]], None]], Awaitable[SessionClient]]


class DeliveryOutcome(Enum):
    """Result of one reply-delivery attempt."""
    DELIVERED = "delivered"
    NOT_AT_GATE = "not_at_gate"
    UNREACHABLE = "unreachable"


async def deliver_session_reply(
    quest: Quest,
    text: str,
    *,
    attach: AttachFn | None = None,
    overall_timeout: float = 10.0,
    grace: float = 2.0,
) -> DeliveryOutcome:
    """Attach, deliver one reply iff the session is at a live gate, then detach.

    DELIVERED — a live (non-_replay) awaiting_user_input arrived and the text was sent.
    NOT_AT_GATE — the replay_complete handshake boundary arrived but no live gate
        followed within `grace`; the session is alive but not awaiting input.
    UNREACHABLE — `overall_timeout` elapsed before the handshake even completed.
    Never blocks longer than `overall_timeout`."""
    attach = attach or build_session_attach(quest, resume=True)
    signal_send, signal_recv = anyio.create_memory_object_stream[str](
        max_buffer_size=8)

    def _on_event(event: dict[str, Any]) -> None:
        if event.get("_replay"):
            return
        etype = event.get("type")
        if etype == "awaiting_user_input":
            with contextlib.suppress(anyio.WouldBlock):
                signal_send.send_nowait("gate")
        elif etype == "replay_complete":
            with contextlib.suppress(anyio.WouldBlock):
                signal_send.send_nowait("boundary")

    async def _decide() -> DeliveryOutcome:
        boundary_seen = False
        with anyio.move_on_after(overall_timeout):
            async for signal in signal_recv:
                if signal == "gate":
                    await client.send_input(text)
                    return DeliveryOutcome.DELIVERED
                if signal == "boundary":
                    boundary_seen = True
                    with anyio.move_on_after(grace):
                        async for post in signal_recv:
                            if post == "gate":
                                await client.send_input(text)
                                return DeliveryOutcome.DELIVERED
                    return DeliveryOutcome.NOT_AT_GATE
        return DeliveryOutcome.NOT_AT_GATE if boundary_seen else DeliveryOutcome.UNREACHABLE

    client = await attach(_on_event)
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(client.receive_events)
            outcome = await _decide()
            tg.cancel_scope.cancel()
    finally:
        signal_send.close()
        signal_recv.close()
        await client.aclose()
    return outcome
