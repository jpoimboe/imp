from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from imp.remote.session.client import SessionClient
from imp.remote.session.protocol import Attach
from imp.remote.session.socket_server import SessionSocket


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_client_attach_sends_change_id_and_title(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    seen: list[Attach] = []
    server = SessionSocket(sock, on_attach=seen.append)
    client = SessionClient(sock, "2026-06-15-xyz", "Add dark mode",
                           on_event=lambda _e: None)
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)
        await client.connect()
        with anyio.fail_after(5):
            while not seen:
                await anyio.sleep(0.02)
        assert seen == [Attach("2026-06-15-xyz", "Add dark mode")]
        await client.aclose()
        await server.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_client_receives_broadcast_event(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionSocket(sock)
    received: list[dict] = []
    client = SessionClient(sock, "c1", "T", on_event=received.append)
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)
        await client.connect()
        tg.start_soon(client.receive_events)
        with anyio.fail_after(5):
            while server.attached_count != 1:
                await anyio.sleep(0.02)
        await server.broadcast({"type": "step_started", "payload": {"step": "plan"}})
        with anyio.fail_after(5):
            while not received:
                await anyio.sleep(0.02)
        assert received == [{"type": "step_started", "payload": {"step": "plan"}}]
        await client.aclose()
        await server.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_client_receive_events_returns_cleanly_on_server_close(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionSocket(sock)
    client = SessionClient(sock, "c1", "T", on_event=lambda _e: None)
    finished: list[bool] = []

    async def _receive() -> None:
        await client.receive_events()
        finished.append(True)

    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)
        await client.connect()
        with anyio.fail_after(5):
            while server.attached_count != 1:
                await anyio.sleep(0.02)
        tg.start_soon(_receive)
        await server.close()
        with anyio.fail_after(5):
            while not finished:
                await anyio.sleep(0.02)
        assert finished == [True]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_client_aclose_is_seen_as_detach(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionSocket(sock)
    client = SessionClient(sock, "c1", "T", on_event=lambda _e: None)
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)
        await client.connect()
        with anyio.fail_after(5):
            while server.attached_count != 1:
                await anyio.sleep(0.02)
        await client.aclose()
        with anyio.fail_after(5):
            while server.attached_count != 0:
                await anyio.sleep(0.02)
        assert server.attached_count == 0
        await server.close()
        tg.cancel_scope.cancel()
