from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.remote.session.relay import SessionRelay
from imp.remote.session.socket_server import SessionSocket


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_relay_forwards_curated_events_with_change_context(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    socket = SessionSocket(sock)
    await socket.open()
    sink: list[dict[str, Any]] = []
    relay = SessionRelay(
        socket_path=sock, change_id="cid", title="T",
        events=("all",), sink=sink.append)
    async with anyio.create_task_group() as tg:
        tg.start_soon(socket.serve)
        tg.start_soon(relay.run)
        with anyio.fail_after(5):
            while socket.attached_count == 0:
                await anyio.sleep(0.02)
        socket.broadcast(
            {"id": "e1", "type": "agent_output", "payload": {"text": "hi"}})
        with anyio.fail_after(5):
            while not sink:
                await anyio.sleep(0.02)
        tg.cancel_scope.cancel()
    await socket.close()
    assert sink[0]["id"] == "e1"
    assert sink[0]["type"] == "agent_output"
    assert sink[0]["change"] == {
        "id": "cid", "title": "T", "step": None, "status": "running"}


@pytest.mark.anyio
async def test_relay_detaches_at_gate_so_session_can_hibernate(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    socket = SessionSocket(sock)
    await socket.open()
    sink: list[dict[str, Any]] = []
    relay = SessionRelay(
        socket_path=sock, change_id="cid", title="T",
        events=("all",), sink=sink.append)
    async with anyio.create_task_group() as tg:
        tg.start_soon(socket.serve)

        async def _emit_gate_once_attached() -> None:
            with anyio.fail_after(5):
                while socket.attached_count == 0:
                    await anyio.sleep(0.02)
            socket.broadcast(
                {"type": "awaiting_user_input", "payload": {"prompt": "go?"}})

        tg.start_soon(_emit_gate_once_attached)
        # run() returns when the relay forwards the gate event and detaches; a relay
        # that pinned residency would never return and this would time out.
        with anyio.fail_after(5):
            await relay.run()
        assert sink[-1]["type"] == "awaiting_user_input"
        # Detached → the socket sees zero clients, so a subscribed change is not
        # pinned resident and can hibernate at its gate.
        with anyio.fail_after(5):
            while socket.attached_count != 0:
                await anyio.sleep(0.02)
        tg.cancel_scope.cancel()
    await socket.close()


@pytest.mark.anyio
async def test_relay_run_raises_when_no_session_is_resident(tmp_path: Path) -> None:
    # The relay attaches to a resident session ONLY; it never spawns one
    # (subscription does not pin residency). No live socket → OSError, not a spawn.
    relay = SessionRelay(
        socket_path=str(tmp_path / "absent.sock"), change_id="cid", title="T",
        events=("all",), sink=lambda _e: None)
    with pytest.raises(OSError):
        await relay.run()
