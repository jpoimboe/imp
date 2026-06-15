from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from imp.remote.daemon.protocol import make_hello
from imp.remote.daemon.server import SouthHub


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_attach_and_detach_hooks_fire_with_correct_args(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "rc.sock")
    attached: list[tuple[str, bool]] = []
    detached: list[str] = []
    hub = SouthHub(
        sock, on_event=lambda cid, line: None,
        on_attach=lambda cid, auto: attached.append((cid, auto)),
        on_detach=lambda cid: detached.append(cid))
    async with anyio.create_task_group() as tg:
        await hub.open()
        tg.start_soon(hub.serve)
        client = await anyio.connect_unix(sock)
        await client.send(
            (make_hello("2026-06-14-abc", "T", autosubscribe=True) + "\n").encode())
        with anyio.fail_after(5):
            while not attached:
                await anyio.sleep(0.02)
        assert attached == [("2026-06-14-abc", True)]
        await client.aclose()
        with anyio.fail_after(5):
            while not detached:
                await anyio.sleep(0.02)
        assert detached == ["2026-06-14-abc"]
        await hub.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_attach_hook_defaults_autosubscribe_false(tmp_path: Path) -> None:
    sock = str(tmp_path / "rc.sock")
    attached: list[tuple[str, bool]] = []
    hub = SouthHub(
        sock, on_event=lambda cid, line: None,
        on_attach=lambda cid, auto: attached.append((cid, auto)))
    async with anyio.create_task_group() as tg:
        await hub.open()
        tg.start_soon(hub.serve)
        client = await anyio.connect_unix(sock)
        await client.send((make_hello("2026-06-14-abc", "T") + "\n").encode())
        with anyio.fail_after(5):
            while not attached:
                await anyio.sleep(0.02)
        assert attached == [("2026-06-14-abc", False)]
        await client.aclose()
        await hub.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_detach_hook_does_not_fire_on_malformed_hello(tmp_path: Path) -> None:
    sock = str(tmp_path / "rc.sock")
    detached: list[str] = []
    hub = SouthHub(
        sock, on_event=lambda cid, line: None,
        on_detach=lambda cid: detached.append(cid))
    async with anyio.create_task_group() as tg:
        await hub.open()
        tg.start_soon(hub.serve)
        client = await anyio.connect_unix(sock)
        await client.send(b'{"type": "not_hello", "change_id": "x"}\n')
        await client.aclose()
        await anyio.sleep(0.1)   # give the server time to process
        assert detached == []
        await hub.close()
        tg.cancel_scope.cancel()
