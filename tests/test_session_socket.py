from __future__ import annotations

from pathlib import Path

import anyio
import pytest
from anyio.abc import SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream

from imp.remote.session.protocol import (
    Attach,
    encode_attach,
    parse_event,
)
from imp.remote.session.socket_server import SessionSocket


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _attach(sock_path: str, change_id: str) -> SocketStream:
    client = await anyio.connect_unix(sock_path)
    await client.send((encode_attach(change_id, "T") + "\n").encode())
    return client


@pytest.mark.anyio
async def test_attach_handshake_registers_client_and_reports_attach(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    attached: list[Attach] = []
    session = SessionSocket(sock, on_attach=attached.append)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        client = await _attach(sock, "2026-06-15-abc")
        with anyio.fail_after(5):
            while session.attached_count != 1:
                await anyio.sleep(0.02)
        assert attached == [Attach("2026-06-15-abc", "T")]
        await client.aclose()
        await session.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_non_attach_opening_frame_is_rejected(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    session = SessionSocket(sock)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        client = await anyio.connect_unix(sock)
        await client.send(b'{"kind": "input", "text": "x"}\n')
        await anyio.sleep(0.2)
        assert session.attached_count == 0
        await client.aclose()
        await session.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_broadcast_reaches_all_concurrent_clients(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    session = SessionSocket(sock)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        a = await _attach(sock, "c1")
        b = await _attach(sock, "c2")
        with anyio.fail_after(5):
            while session.attached_count != 2:
                await anyio.sleep(0.02)
        await session.broadcast({"type": "agent_output", "payload": {"n": 1}})
        for client in (a, b):
            buf = BufferedByteReceiveStream(client)
            line = (await buf.receive_until(b"\n", 1 << 20)).decode()
            assert parse_event(line) == {"type": "agent_output", "payload": {"n": 1}}
        await a.aclose()
        await b.aclose()
        await session.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_socket_drop_is_treated_as_detach(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    detaches = 0

    def _on_detach() -> None:
        nonlocal detaches
        detaches += 1

    session = SessionSocket(sock, on_detach=_on_detach)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        client = await _attach(sock, "c1")
        with anyio.fail_after(5):
            while session.attached_count != 1:
                await anyio.sleep(0.02)
        await client.aclose()
        with anyio.fail_after(5):
            while session.attached_count != 0:
                await anyio.sleep(0.02)
        assert detaches == 1
        await session.close()
        tg.cancel_scope.cancel()
