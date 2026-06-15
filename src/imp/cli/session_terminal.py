"""Thin terminal session client: render the daemon's event stream, answer its gates.

Composes the Plan 1-7 primitives into the client half of the per-change session
daemon. `run_terminal_client` attaches to a change's session daemon (via an
injected attach thunk), streams the daemon's events into a live event renderer
(LiveDriver), forwards a local interrupt gesture as a control frame
(SessionControls, without killing the client), and answers each wait_for_user
gate by soliciting one reply and sending it back as an input frame. It returns
when the daemon's run reaches a terminal state and drops the socket. The CLI flip
(a later plan) builds the attach thunk, the LiveDriver, the InterruptCoordinator,
and the solicit callback, and replaces the in-process run_change call in
`imp new` / `imp resume` with this loop.
"""
from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import Any

import anyio

from imp.cli.session_controls import SessionControls
from imp.cli.signals import InterruptCoordinator
from imp.remote.session.client import SessionClient

AttachFn = Callable[[Callable[[dict[str, Any]], None]], Awaitable[SessionClient]]


async def run_terminal_client(
    *,
    attach: AttachFn,
    driver: Any,
    coord: InterruptCoordinator,
    solicit: Callable[[str], Awaitable[str]],
) -> None:
    gate_send, gate_recv = anyio.create_memory_object_stream[str](
        max_buffer_size=math.inf)

    def _on_event(event: dict[str, Any]) -> None:
        driver.on_event(event)
        if event.get("type") == "awaiting_user_input":
            prompt = (event.get("payload") or {}).get("prompt") or ""
            gate_send.send_nowait(prompt)

    coord.escalation_event = anyio.Event()
    coord.set_escalation_hook(coord.escalation_event.set)

    client = await attach(_on_event)
    try:
        # Constructed for its side effect: binds the interrupt-forward runner onto coord.
        SessionControls(client, coord)
        async with anyio.create_task_group() as tg:
            async def _answer_gates() -> None:
                async for prompt in gate_recv:
                    await client.send_input(await solicit(prompt))

            async def _watch_force_quit() -> None:
                # cancel scope → finally → client.aclose; daemon detaches and runs on
                await coord.escalation_event.wait()
                tg.cancel_scope.cancel()

            tg.start_soon(_answer_gates)
            tg.start_soon(_watch_force_quit)
            await client.receive_events()
            tg.cancel_scope.cancel()
    finally:
        coord.detach_runner()
        await gate_send.aclose()
        gate_recv.close()
        driver.finish()
        await client.aclose()
