from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest
from anyio.abc import SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream

from imp.remote.session.protocol import (
    encode_attach,
    encode_control,
    encode_input,
    parse_event,
)
from imp.remote.session.server import SessionServer


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _attach(sock_path: str, change_id: str) -> SocketStream:
    client = await anyio.connect_unix(sock_path)
    await client.send((encode_attach(change_id, "T") + "\n").encode())
    return client


@pytest.mark.anyio
async def test_run_fn_events_are_broadcast_to_attached_client(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    release = anyio.Event()
    server = SessionServer(sock)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count < 1:
                await anyio.sleep(0.02)
        on_event({"type": "agent_output", "payload": {"text": "hi"}})
        await release.wait()
        return "ok"

    async with anyio.create_task_group() as tg:
        await server.open()
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await server.serve(run_fn)

        tg.start_soon(_serve)
        client = await _attach(sock, "c1")
        buf = BufferedByteReceiveStream(client)
        line = (await buf.receive_until(b"\n", 1 << 20)).decode()
        assert parse_event(line) == {
            "type": "agent_output", "payload": {"text": "hi"}}
        release.set()
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "ok"
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_first_client_input_satisfies_the_gate_and_returns(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock)
    received: list[str] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        reply = await user_chat_input_fn("")
        received.append(reply)
        return reply

    async with anyio.create_task_group() as tg:
        await server.open()
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await server.serve(run_fn)

        tg.start_soon(_serve)
        client = await _attach(sock, "c1")
        with anyio.fail_after(5):
            while server.attached_count != 1:
                await anyio.sleep(0.02)
        await anyio.sleep(0.05)  # let run_fn reach the gate (open next_input)
        await client.send((encode_input("ship it") + "\n").encode())
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "ship it"
        assert received == ["ship it"]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_interrupt_control_pauses_the_coordinator(tmp_path: Path) -> None:
    from imp.cli.signals import InterruptCoordinator

    sock = str(tmp_path / "s.sock")
    coord = InterruptCoordinator()
    release = anyio.Event()
    server = SessionServer(sock, coordinator=coord)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await release.wait()
        return "done"

    async with anyio.create_task_group() as tg:
        await server.open()
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await server.serve(run_fn)

        tg.start_soon(_serve)
        client = await _attach(sock, "c1")
        with anyio.fail_after(5):
            while server.attached_count != 1:
                await anyio.sleep(0.02)
        assert not coord.should_pause
        await client.send((encode_control("interrupt") + "\n").encode())
        with anyio.fail_after(5):
            while not coord.should_pause:
                await anyio.sleep(0.02)
        assert coord.should_pause
        release.set()
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "done"
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_abandon_control_invokes_the_callback(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    release = anyio.Event()
    abandoned: list[bool] = []
    server = SessionServer(sock, on_abandon=lambda: abandoned.append(True))

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await release.wait()
        return "done"

    async with anyio.create_task_group() as tg:
        await server.open()
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await server.serve(run_fn)

        tg.start_soon(_serve)
        client = await _attach(sock, "c1")
        with anyio.fail_after(5):
            while server.attached_count != 1:
                await anyio.sleep(0.02)
        await client.send((encode_control("abandon") + "\n").encode())
        with anyio.fail_after(5):
            while not abandoned:
                await anyio.sleep(0.02)
        assert abandoned == [True]
        release.set()
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_lock_is_held_during_serve_and_released_after(tmp_path: Path) -> None:
    from imp.engine.state import ProjectLock

    sock = str(tmp_path / "s.sock")
    lock_path = tmp_path / "lock"
    release = anyio.Event()
    server = SessionServer(sock, lock=ProjectLock(lock_path))

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await release.wait()
        return "done"

    async with anyio.create_task_group() as tg:
        await server.open()
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await server.serve(run_fn)

        tg.start_soon(_serve)
        await anyio.sleep(0.05)  # let serve acquire the lock and enter run_fn
        assert lock_path.exists()  # ProjectLock writes the holder-pid file while held
        release.set()
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "done"
        assert not lock_path.exists()  # released on serve() return
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_gate_with_zero_clients_hibernates(tmp_path: Path) -> None:
    from imp.agents.tools.wait_for_user import AwaitingUserInput

    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=True)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        try:
            await user_chat_input_fn("")
        except AwaitingUserInput:
            return "hibernated"
        return "resident"

    async with anyio.create_task_group() as tg:
        await server.open()
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await server.serve(run_fn)

        tg.start_soon(_serve)
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "hibernated"
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_gate_with_attached_client_returns_first_input(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=True)
    received: list[str] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count < 1:
                await anyio.sleep(0.02)
        reply = await user_chat_input_fn("")
        received.append(reply)
        return reply

    async with anyio.create_task_group() as tg:
        await server.open()
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await server.serve(run_fn)

        tg.start_soon(_serve)
        client = await _attach(sock, "c1")
        with anyio.fail_after(5):
            while server.attached_count != 1:
                await anyio.sleep(0.02)
        await anyio.sleep(0.05)  # let run_fn reach the gate (open next_input)
        await client.send((encode_input("ship it") + "\n").encode())
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "ship it"
        assert received == ["ship it"]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_gate_hibernates_when_last_client_detaches(tmp_path: Path) -> None:
    from imp.agents.tools.wait_for_user import AwaitingUserInput

    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=True)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count < 1:
                await anyio.sleep(0.02)
        try:
            await user_chat_input_fn("")
        except AwaitingUserInput:
            return "hibernated"
        return "resident"

    async with anyio.create_task_group() as tg:
        await server.open()
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await server.serve(run_fn)

        tg.start_soon(_serve)
        client = await _attach(sock, "c1")
        with anyio.fail_after(5):
            while server.attached_count != 1:
                await anyio.sleep(0.02)
        await anyio.sleep(0.05)  # let the gate open while resident
        await client.aclose()
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "hibernated"
        tg.cancel_scope.cancel()
