# tests/test_daemon_southhub_register.py
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from imp.remote.daemon.protocol import make_hello
from imp.remote.daemon.server import SouthHub
from tests.async_wait import wait_until


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_hub_registers_change_on_hello(tmp_path: Path) -> None:
    sock = str(tmp_path / "rc.sock")
    hub = SouthHub(sock, on_event=lambda cid, line: None)
    async with anyio.create_task_group() as tg:
        await hub.open()
        tg.start_soon(hub.serve)
        client = await anyio.connect_unix(sock)
        await client.send((make_hello("2026-06-13-abc", "T") + "\n").encode())
        await wait_until(lambda: "2026-06-13-abc" in hub.live_ids())
        assert hub.live_ids() == {"2026-06-13-abc"}
        await client.aclose()
        await wait_until(lambda: not hub.live_ids())
        assert hub.live_ids() == set()
        await hub.close()
        tg.cancel_scope.cancel()
