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

import contextlib
import math
import signal
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
    change_id: str = "",
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

    # Arm the Ctrl-\ (SIGQUIT) trap BEFORE attaching so a deliberate detach works
    # any time, including mid-grind with no prompt up. open_signal_receiver is a
    # sync context manager that installs the loop handler on enter (replacing
    # SIGQUIT's core-dump default) and restores it on exit.
    sigquit = getattr(signal, "SIGQUIT", None)
    with contextlib.ExitStack() as stack:
        sigquit_signals = (
            stack.enter_context(anyio.open_signal_receiver(sigquit))
            if sigquit is not None else None)
        client = await attach(_on_event)
        # Snapshot should_pause AFTER attach but BEFORE binding the runner.
        # An interrupt that arrived during attach() found fn=None in signal_pause
        # and wasn't forwarded to the daemon. Detect it here so the flush task
        # can re-send CONTROL_INTERRUPT once the runner is bound.
        _interrupt_before_runner = coord.should_pause
        try:
            controls = SessionControls(client, coord)
            async with anyio.create_task_group() as tg:
                async def _answer_gates() -> None:
                    async for prompt in gate_recv:
                        while True:
                            reply = await solicit(prompt)
                            if reply.strip() == "/abandon":
                                confirm = await solicit(
                                    f"abandon `{change_id}`? [y/N]")
                                if confirm.strip().lower() in ("y", "yes"):
                                    await controls.abandon()
                                    return
                                continue
                            await client.send_input(reply)
                            break

                async def _watch_force_quit() -> None:
                    # cancel scope → finally → client.aclose; daemon detaches and runs on
                    await coord.escalation_event.wait()
                    tg.cancel_scope.cancel()

                async def _watch_sigquit() -> None:
                    # Ctrl-\ deliberate detach: same teardown as force-quit — trigger
                    # the escalation event so _watch_force_quit handles the cancel.
                    # This avoids a race in anyio's asyncio backend where the sigquit
                    # wakeup Handle lands in _ready before the socket-reader Handle,
                    # causing InvalidStateError when cancel fires before set_result.
                    if sigquit_signals is None:
                        return
                    async for _sig in sigquit_signals:
                        coord.escalation_event.set()
                        break

                async def _flush_pending_interrupt() -> None:
                    if _interrupt_before_runner:
                        from imp.remote.session.protocol import CONTROL_INTERRUPT
                        await client.send_control(CONTROL_INTERRUPT)

                tg.start_soon(_answer_gates)
                tg.start_soon(_watch_force_quit)
                tg.start_soon(_watch_sigquit)
                tg.start_soon(_flush_pending_interrupt)
                await client.receive_events()
                tg.cancel_scope.cancel()
        finally:
            coord.detach_runner()
            coord.set_escalation_hook(None)
            await gate_send.aclose()
            gate_recv.close()
            driver.finish()
            await client.aclose()
