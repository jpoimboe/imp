"""Live fan-out of the coordinator's interrupt records to attached clients."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.engine.quest import Quest
from imp.engine.state import State
from imp.paths import project_state_dir
from imp.remote.session.client import SessionClient
from imp.remote.session.daemon import serve_quest_session
from imp.remote.session.discover import session_socket_path
from tests.async_wait import HANG_BACKSTOP, wait_until


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_quest(tmp_path: Path, quest_id: str) -> Quest:
    quest = Quest(
        id=quest_id,
        title="T",
        base_branch="main",
        control_root=tmp_path,
        project_root=tmp_path,
        quest_dir=project_state_dir(tmp_path) / "quests" / quest_id,
    )
    State(id=quest_id, step=None, step_attempt=0, status="paused").save(
        quest.state_dir)
    return quest


def _journal(quest: Quest) -> list[dict[str, Any]]:
    if not quest.events_path.exists():
        return []
    return [json.loads(line)
            for line in quest.events_path.read_text().splitlines() if line.strip()]


async def _wait_for_socket(sock: str) -> None:
    with anyio.fail_after(HANG_BACKSTOP):  # deadlock backstop only, never a tuned window
        while True:
            try:
                stream = await anyio.connect_unix(sock)
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                await anyio.sleep(0.02)
                continue
            await stream.aclose()
            return


@pytest.mark.anyio
async def test_attached_client_sees_interrupt_events_live(tmp_path: Path) -> None:
    quest = _make_quest(tmp_path, "2026-07-25-brdcst")
    sock = str(session_socket_path(quest.id))
    live: list[dict[str, Any]] = []
    pulses: list[int] = []

    async def fake_run(
        _quest: Quest, *, on_event: Any, user_chat_input_fn: Any, coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> str:
        coordinator.add_activity_listener(lambda: pulses.append(1))
        # cli_attached is journaled on the 0 -> 1 leased attach, by which point the
        # client is registered for broadcast
        await wait_until(
            lambda: any(e["type"] == "cli_attached" for e in _journal(quest)))

        async def _interrupt() -> None:
            return None

        with coordinator.runner_attached(_interrupt, agent_id="step:p/s"):
            await coordinator.signal_pause(reason="ctrl-c", via="frame")
            coordinator.note_yield()  # disarm the 5s watchdog; delivery is unaffected
            await wait_until(lambda: any(
                e["type"] == "interrupt_delivered" for e in _journal(quest)))
        # Ordered sentinel: the socket is FIFO per client, so this frame arriving
        # proves every earlier frame was parsed too.
        on_event({"type": "agent_result", "payload": {"text": "done"}})
        return "ok"

    async with anyio.create_task_group() as tg:
        run_done = anyio.Event()

        async def _serve() -> None:
            await serve_quest_session(quest, run_fn=fake_run, hibernate=False)
            run_done.set()

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        client = SessionClient(sock, quest.id, quest.title,
                               on_event=live.append, lease=True)
        await client.connect()
        tg.start_soon(client.receive_events)
        with anyio.fail_after(HANG_BACKSTOP):  # deadlock backstop only, never a tuned window
            await run_done.wait()
        # Poll for the client having PARSED the frames: closing the stream first
        # would drop anything still unparsed in its receive loop.
        await wait_until(lambda: any(e["type"] == "agent_result" for e in live))
        await client.aclose()
        tg.cancel_scope.cancel()

    interrupts = [e for e in live if e["type"].startswith("interrupt_")]
    assert [e["type"] for e in interrupts] == [
        "interrupt_requested", "interrupt_delivered"]
    assert all("_replay" not in e for e in interrupts)   # live, not replayed
    assert interrupts[1]["payload"]["agent_id"] == "step:p/s"
    assert interrupts[1]["payload"]["outcome"] == "ok"
    journal = _journal(quest)
    assert sum(1 for e in journal if e["type"] == "interrupt_requested") == 1
    assert sum(1 for e in journal if e["type"] == "interrupt_delivered") == 1
    assert pulses == []   # the fan-out must not pulse the inactivity watchdog
