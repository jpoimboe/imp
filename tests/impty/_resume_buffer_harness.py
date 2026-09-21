"""Child process for the buffered-latch / stop-ack pty tests.

Drives the REAL client stack — LiveStatus + LiveDriver with the 100ms spinner, an
InterruptCoordinator wired exactly as session_run.py wires it, an in-process
SessionServer/SessionClient pair, and the real
make_buffered_solicit(make_interactive_solicit(...)) composition — over the pty it was
spawned on, advancing one phase per marker file the parent test drops into the control
dir (argv[1]) so the parent never races the child's timeline.

argv: [1] control dir (markers + socket), [2] control root, [3] quest id,
[4] mode ("interrupt" | "bare").
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio

from imp.cli.inert_resume import make_buffered_solicit
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.session_run import make_interactive_solicit
from imp.cli.session_terminal import run_terminal_client
from imp.cli.signals import InterruptCoordinator
from imp.engine.completion import INTERRUPT_STEER_HINT
from imp.engine.quest import Quest
from imp.remote.session.client import SessionClient
from imp.remote.session.server import SessionServer

BUFFERED = "BUFFEREDANSWER"
BLOCK = "NARRATIVEBLOCK"


def _ev(eid: str, etype: str, agent_id: str | None = None, **payload: Any) -> dict[str, Any]:
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


def _message(eid: str, text: str) -> dict[str, Any]:
    return _ev(eid, "agent_message", agent_id="step:plan/planner",
               raw={"content": [{"type": "text", "text": text}]}, usage={})


async def _await_marker(marker: Path) -> None:
    while not marker.exists():
        await anyio.sleep(0.02)


async def main() -> None:
    ctl = Path(sys.argv[1])
    quest = Quest.load(Path(sys.argv[2]), sys.argv[3])
    mode = sys.argv[4]
    sock = str(ctl / "session.sock")

    status = LiveStatus(sys.stdout)
    driver = LiveDriver(stream=sys.stdout, color=False, width=80, status=status)
    coord = InterruptCoordinator()
    coord.set_ack_hook(driver.show_stop_ack)     # exactly session_run.py's wiring
    # No server coordinator: the client's gesture sends a real CONTROL_INTERRUPT, which
    # must paint the ack without perturbing run_fn.
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        while server.attached_count == 0:
            await anyio.sleep(0.02)
        on_event(_ev("1", "quest_created", title=quest.title, quest_id=1))
        on_event(_ev("2", "step_started", step="planner", kind="agent",
                     phase="plan", model="m", provider="anthropic"))
        on_event(_message("3", f"{BLOCK} 0"))
        await _await_marker(ctl / "gate")
        if mode == "interrupt":
            on_event({"type": "awaiting_user_input",
                      "payload": {"steer_hint": INTERRUPT_STEER_HINT,
                                  "interrupt": True}})
        else:
            on_event({"type": "awaiting_user_input",
                      "payload": {"steer_hint": "", "interrupt": False}})
        reply = await user_chat_input_fn("")

        if mode == "bare":
            # Keep the run alive emitting output so the footer repaints while the
            # scrollback scrolls; the parent's `quit` marker ends it.
            await _await_marker(ctl / "output")
            seq = 0
            while not (ctl / "quit").exists():
                seq += 1
                on_event(_message(f"m{seq}", f"{BLOCK} {seq}"))
                await anyio.sleep(0.15)
        return str(reply)

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, quest.id, quest.title, on_event=on_event)
        await client.connect()
        return client

    solicit = make_buffered_solicit(
        BUFFERED,
        make_interactive_solicit(quest, status=status, driver=driver, coord=coord))

    async def _gesture() -> None:
        await _await_marker(ctl / "gesture")
        await coord.signal_pause(reason="ctrl-c")   # -> _note_gesture_ack -> the ack

    async with anyio.create_task_group() as tg:
        tg.start_soon(status.tick)                  # the real 100ms spinner
        await server.open()
        tg.start_soon(server.serve, run_fn)
        tg.start_soon(_gesture)
        await run_terminal_client(
            attach=_attach, driver=driver, coord=coord, solicit=solicit,
            quest_id=quest.id, control_root=quest.control_root,
            leave_state={}, leave_mode="new",
            local_echo=driver.seed_pending_reply)
        tg.cancel_scope.cancel()


anyio.run(main)
