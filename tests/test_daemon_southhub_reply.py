from __future__ import annotations

from pathlib import Path

import anyio
import pytest
from anyio.streams.buffered import BufferedByteReceiveStream

from imp.remote.daemon.protocol import make_hello
from imp.remote.daemon.server import SouthHub


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _connect_and_hello(sock: str, change_id: str) -> anyio.abc.SocketStream:
    client = await anyio.connect_unix(sock)
    await client.send((make_hello(change_id, "T") + "\n").encode())
    return client


@pytest.mark.anyio
async def test_reply_reaches_only_the_target_connection(tmp_path: Path) -> None:
    sock = str(tmp_path / "rc.sock")
    hub = SouthHub(sock, on_event=lambda cid, line: None)
    async with anyio.create_task_group() as tg:
        await hub.open()
        tg.start_soon(hub.serve)
        a = await _connect_and_hello(sock, "id-a")
        b = await _connect_and_hello(sock, "id-b")
        with anyio.fail_after(5):
            while hub.live_ids() != {"id-a", "id-b"}:
                await anyio.sleep(0.02)

        assert await hub.send_reply("id-a", "go-a") is True
        a_buf = BufferedByteReceiveStream(a)
        with anyio.fail_after(5):
            line = await a_buf.receive_until(b"\n", 4096)
        assert line == b"go-a"

        # Verify b did NOT receive the reply (isolation check).
        with pytest.raises(TimeoutError):
            with anyio.fail_after(0.1):
                await b.receive()

        # An unknown change_id is a no-op (returns False), not an error.
        assert await hub.send_reply("id-unknown", "nope") is False

        await a.aclose()
        await b.aclose()
        await hub.close()
        tg.cancel_scope.cancel()
