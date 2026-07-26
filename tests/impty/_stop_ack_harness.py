"""Child process for the sticky stop-ack pty test.

Drives the REAL LiveDriver / LiveStatus over the pty it was spawned on, with the
real 100ms spinner tick running, and advances one phase per marker file the
parent test drops into the control dir (argv[1]) — so the parent never races the
child's timeline. Not a test module (no test_ prefix): pytest never collects it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import anyio

from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.signals import InterruptCoordinator


def _ev(eid: str, etype: str, agent_id: str | None = None, **payload: Any) -> dict[str, Any]:
    return {"id": eid, "agent_id": agent_id, "type": etype, "payload": payload}


async def _await_marker(marker: Path) -> None:
    while not marker.exists():
        await anyio.sleep(0.02)


async def main() -> None:
    ctl = Path(sys.argv[1])
    status = LiveStatus(sys.stdout)
    driver = LiveDriver(stream=sys.stdout, color=False, width=80, status=status)
    coord = InterruptCoordinator()
    coord.set_ack_hook(driver.show_stop_ack)        # exactly session_run.py's wiring
    driver.on_event(_ev("1", "quest_created", title="x", quest_id=1))
    driver.on_event(_ev("2", "step_started", step="planner", kind="agent",
                        phase="plan", model="m", provider="anthropic"))

    async with anyio.create_task_group() as tg:
        tg.start_soon(status.tick)                  # the real 100ms spinner
        await _await_marker(ctl / "gesture")
        await coord.signal_pause(reason="ctrl-c")   # -> _note_gesture_ack -> the ack

        await _await_marker(ctl / "events")
        # The gesture's own records, live: each drives a _refresh_status.
        driver.on_event(_ev("3", "interrupt_requested", reason="ctrl-c", via="signal"))
        driver.on_event(_ev("4", "interrupt_delivered", agent_id="step:plan/planner",
                            tier="cooperative", outcome="ok"))

        await _await_marker(ctl / "block")
        driver.on_event(_ev("5", "agent_message", agent_id="step:plan/planner",
                            raw={"content": [{"type": "text", "text": "NARRATIVEBLOCK"}]},
                            usage={}))

        await _await_marker(ctl / "prompt")
        with status.suspend():                      # the steer prompt takes the screen
            driver.prepare_for_prompt()             # slash.py's order: inside the suspend
            sys.stdout.write("PROMPTHERE")
            sys.stdout.flush()
            await _await_marker(ctl / "quit")
        driver.finish()
        tg.cancel_scope.cancel()


anyio.run(main)
