"""Deliver one reply into a change's session daemon (attach + inject + detach)."""
from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import Any

import anyio

from imp.engine.change import Change
from imp.remote.session.attach import build_session_attach
from imp.remote.session.client import SessionClient

AttachFn = Callable[[Callable[[dict[str, Any]], None]], Awaitable[SessionClient]]


async def deliver_session_reply(
    change: Change,
    text: str,
    *,
    attach: AttachFn | None = None,
    gate_timeout: float = 60.0,
) -> None:
    """Deliver one reply into change's session daemon, then detach."""
    attach = attach or build_session_attach(change, resume=True)
    gate_send, gate_recv = anyio.create_memory_object_stream[str](
        max_buffer_size=math.inf)

    def _on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "awaiting_user_input":
            gate_send.send_nowait("")

    client = await attach(_on_event)
    try:
        with anyio.fail_after(gate_timeout):
            async with anyio.create_task_group() as tg:
                async def _answer() -> None:
                    await gate_recv.receive()
                    # gate is open only while next_input() runs; send after the event
                    await client.send_input(text)
                    tg.cancel_scope.cancel()

                tg.start_soon(_answer)
                await client.receive_events()
                tg.cancel_scope.cancel()
    finally:
        await gate_send.aclose()
        await gate_recv.aclose()
        await client.aclose()
