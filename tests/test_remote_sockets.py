from __future__ import annotations

import ast
import asyncio
import socket
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest
from anyio.abc import SocketAttribute, SocketListener, SocketStream

from imp.remote.sockets import connect_unix, create_unix_listener
from tests.async_wait import HANG_BACKSTOP


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@contextmanager
def _loop_exceptions() -> Iterator[list[dict[str, Any]]]:
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    recorded: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, ctx: recorded.append(ctx))
    try:
        yield recorded
    finally:
        loop.set_exception_handler(previous)


@asynccontextmanager
async def _served(
    path: Path,
) -> AsyncIterator[tuple[SocketListener, socket.socket, SocketStream]]:
    listener = await create_unix_listener(path)
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.connect(str(path))
    stream = await listener.accept()
    try:
        yield listener, peer, stream
    finally:
        await stream.aclose()
        peer.close()
        await listener.aclose()


async def _cancel_as_fd_turns_ready(
    waiter: Callable[[], Any], make_ready: Callable[[], Any],
) -> None:
    # The selector queues the fd's callback during the asyncio.sleep(0) turn; the cancel
    # lands before it runs — the window where a bare future.set_result reader raises.
    async with anyio.create_task_group() as tg:
        tg.start_soon(waiter)
        await anyio.wait_all_tasks_blocked()
        make_ready()
        await asyncio.sleep(0)
        tg.cancel_scope.cancel()
    await anyio.sleep(0.01)


@pytest.mark.anyio
async def test_receive_cancelled_as_peer_sends(tmp_path: Path) -> None:
    async with _served(tmp_path / "s.sock") as (_, peer, stream):
        with _loop_exceptions() as recorded:
            await _cancel_as_fd_turns_ready(stream.receive, lambda: peer.send(b"x"))
        assert recorded == []
        assert await stream.receive() == b"x"


@pytest.mark.anyio
async def test_receive_cancelled_as_peer_resets(tmp_path: Path) -> None:
    async with _served(tmp_path / "s.sock") as (_, peer, stream):
        await stream.send(b"unread")
        with _loop_exceptions() as recorded:
            await _cancel_as_fd_turns_ready(stream.receive, peer.close)
        assert recorded == []
        with pytest.raises(anyio.BrokenResourceError):
            await stream.receive()


@pytest.mark.anyio
async def test_accept_cancelled_as_client_connects(tmp_path: Path) -> None:
    path = tmp_path / "s.sock"
    listener = await create_unix_listener(path)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with _loop_exceptions() as recorded:
            await _cancel_as_fd_turns_ready(listener.accept, lambda: client.connect(str(path)))
        assert recorded == []
        accepted = await listener.accept()
        await accepted.aclose()
    finally:
        client.close()
        await listener.aclose()


@pytest.mark.anyio
async def test_aclose_wakes_parked_receive_with_closed_resource_error(tmp_path: Path) -> None:
    async with _served(tmp_path / "s.sock") as (_, _peer, stream):
        raised: list[BaseException] = []

        async def reader() -> None:
            try:
                await stream.receive()
            except anyio.ClosedResourceError as exc:
                raised.append(exc)

        with anyio.fail_after(HANG_BACKSTOP):
            async with anyio.create_task_group() as tg:
                tg.start_soon(reader)
                await anyio.wait_all_tasks_blocked()
                await stream.aclose()
        assert len(raised) == 1


@pytest.mark.anyio
async def test_receive_after_peer_close_raises_end_of_stream(tmp_path: Path) -> None:
    async with _served(tmp_path / "s.sock") as (_, peer, stream):
        peer.close()
        with pytest.raises(anyio.EndOfStream):
            await stream.receive()


@pytest.mark.anyio
async def test_raw_socket_attribute_is_the_underlying_socket(tmp_path: Path) -> None:
    path = tmp_path / "s.sock"
    async with _served(path) as (listener, _, _):
        client = await connect_unix(path)
        try:
            raw = client.extra(SocketAttribute.raw_socket)
            accepted = await listener.accept()
            raw.send(b"via raw")
            assert await accepted.receive() == b"via raw"
            await accepted.aclose()
        finally:
            await client.aclose()


_SRC = Path(__file__).resolve().parents[1] / "src" / "imp"
_UNSAFE_FACTORIES = frozenset({"connect_unix", "create_unix_listener"})


def _opens_unsafe_socket(node: ast.AST) -> bool:
    if isinstance(node, ast.ImportFrom):
        return node.module == "anyio" and any(a.name in _UNSAFE_FACTORIES for a in node.names)
    if isinstance(node, ast.Attribute):
        return node.attr == "from_socket" or (
            node.attr in _UNSAFE_FACTORIES
            and isinstance(node.value, ast.Name) and node.value.id == "anyio")
    return False


def test_unix_sockets_open_only_through_imp_remote_sockets() -> None:
    offenders = [
        f"{path.relative_to(_SRC)}:{node.lineno}"
        for path in sorted(_SRC.rglob("*.py"))
        if path != _SRC / "remote" / "sockets.py"
        for node in ast.walk(ast.parse(path.read_text()))
        if _opens_unsafe_socket(node)
    ]
    assert offenders == []
