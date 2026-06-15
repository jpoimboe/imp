from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from imp.cli.session_controls import SessionControls
from imp.cli.signals import InterruptCoordinator
from imp.remote.session.client import SessionClient
from imp.remote.session.socket_server import SessionSocket


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_interrupt_gesture_forwards_interrupt_control_without_detaching(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    actions: list[str] = []
    server = SessionSocket(sock, on_control=actions.append)
    client = SessionClient(sock, "c1", "T", on_event=lambda _e: None)
    coord = InterruptCoordinator()
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)
        await client.connect()
        with anyio.fail_after(5):
            while server.attached_count != 1:
                await anyio.sleep(0.02)
        SessionControls(client, coord)
        await coord.signal_pause()
        with anyio.fail_after(5):
            while not actions:
                await anyio.sleep(0.02)
        assert actions == ["interrupt"]
        assert server.attached_count == 1
        await coord.signal_pause()
        with anyio.fail_after(5):
            while len(actions) != 2:
                await anyio.sleep(0.02)
        assert actions == ["interrupt", "interrupt"]
        await client.aclose()
        await server.close()
        tg.cancel_scope.cancel()
