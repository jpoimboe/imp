from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from imp.remote.daemon.protocol import make_hello
from imp.remote.daemon.runner import _make_on_attach
from imp.remote.daemon.server import SouthHub
from imp.remote.daemon.subscriptions import Subscriptions


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_autosubscribe_hello_subscribes_and_detach_keeps_it(tmp_path: Path) -> None:
    subs = Subscriptions()
    sock = str(tmp_path / "rc.sock")
    hub = SouthHub(
        sock, on_event=lambda cid, line: None, on_attach=_make_on_attach(subs))
    async with anyio.create_task_group() as tg:
        await hub.open()
        tg.start_soon(hub.serve)
        client = await anyio.connect_unix(sock)
        await client.send((make_hello("c1", "T", autosubscribe=True) + "\n").encode())
        with anyio.fail_after(5):
            while not subs.contains("c1"):
                await anyio.sleep(0.02)
        assert subs.contains("c1")
        await client.aclose()
        await anyio.sleep(0.1)            # detach has no on_detach wired
        assert subs.contains("c1")
        await hub.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_non_autosubscribe_hello_does_not_subscribe(tmp_path: Path) -> None:
    subs = Subscriptions()
    sock = str(tmp_path / "rc.sock")
    hub = SouthHub(
        sock, on_event=lambda cid, line: None, on_attach=_make_on_attach(subs))
    async with anyio.create_task_group() as tg:
        await hub.open()
        tg.start_soon(hub.serve)
        client = await anyio.connect_unix(sock)
        await client.send((make_hello("c2", "T") + "\n").encode())
        await anyio.sleep(0.2)
        assert subs.contains("c2") is False
        await client.aclose()
        await hub.close()
        tg.cancel_scope.cancel()
