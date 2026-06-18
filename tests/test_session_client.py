from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from imp.remote.session.client import SessionClient
from imp.remote.session.protocol import CONTROL_INTERRUPT, Attach
from imp.remote.session.socket_server import SessionSocket


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_client_attach_sends_change_id_and_title(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    seen: list[Attach] = []
    server = SessionSocket(sock, on_attach=lambda hello, _send: seen.append(hello))
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
        server.broadcast({"type": "step_started", "payload": {"step": "plan"}})
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


@pytest.mark.anyio
async def test_client_input_satisfies_server_gate(tmp_path: Path) -> None:
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
        got: list[str] = []

        async def _gate() -> None:
            got.append(await server.next_input())

        tg.start_soon(_gate)
        await anyio.sleep(0.05)  # let the gate open (next_input is awaiting)
        await client.send_input("ship it")
        with anyio.fail_after(5):
            while not got:
                await anyio.sleep(0.02)
        assert got == ["ship it"]
        await client.aclose()
        await server.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_client_control_invokes_server_on_control(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    actions: list[str] = []
    server = SessionSocket(sock, on_control=actions.append)
    client = SessionClient(sock, "c1", "T", on_event=lambda _e: None)
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)
        await client.connect()
        with anyio.fail_after(5):
            while server.attached_count != 1:
                await anyio.sleep(0.02)
        await client.send_control("interrupt")
        with anyio.fail_after(5):
            while not actions:
                await anyio.sleep(0.02)
        assert actions == ["interrupt"]
        await client.aclose()
        await server.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_client_send_control_rejects_unknown_action(tmp_path: Path) -> None:
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
        with pytest.raises(ValueError, match="unknown control action"):
            await client.send_control("explode")
        await client.aclose()
        await server.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_concurrent_send_input_and_control_do_not_collide(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    actions: list[str] = []
    inputs: list[str] = []
    server = SessionSocket(sock, on_control=actions.append)
    client = SessionClient(sock, "c1", "T", on_event=lambda _e: None)
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)
        await client.connect()
        with anyio.fail_after(5):
            while server.attached_count != 1:
                await anyio.sleep(0.02)

        real_send = client._stream.send  # type: ignore[union-attr]
        block = anyio.Event()
        max_concurrent = 0
        current_concurrent = 0

        async def _slow_send(data: bytes) -> None:
            nonlocal current_concurrent, max_concurrent
            current_concurrent += 1
            max_concurrent = max(max_concurrent, current_concurrent)
            await block.wait()
            current_concurrent -= 1
            await real_send(data)

        client._stream.send = _slow_send  # type: ignore[method-assign, union-attr]

        async def _gate() -> None:
            inputs.append(await server.next_input())

        tg.start_soon(_gate)
        await anyio.sleep(0.05)
        async with anyio.create_task_group() as send_tg:
            send_tg.start_soon(client.send_input, "reply")
            send_tg.start_soon(client.send_control, CONTROL_INTERRUPT)
            await anyio.sleep(0.05)
            block.set()
        with anyio.fail_after(5):
            while not (inputs and actions):
                await anyio.sleep(0.02)
        assert max_concurrent == 1, "lock must serialize concurrent sends"
        assert inputs == ["reply"]
        assert actions == ["interrupt"]
        await client.aclose()
        await server.close()
        tg.cancel_scope.cancel()
