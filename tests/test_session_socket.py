from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from anyio.abc import SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream

from imp.remote.session.protocol import (
    Attach,
    Input,
    SkillRequest,
    encode_attach,
    encode_control,
    encode_input,
    encode_set_lease,
    encode_skill_request,
    parse_event,
)
from imp.remote.session.socket_server import SessionSocket, _Client
from tests.async_wait import wait_until


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _attach(sock_path: str, quest_id: str) -> SocketStream:
    client = await anyio.connect_unix(sock_path)
    await client.send((encode_attach(quest_id, "T") + "\n").encode())
    return client


@pytest.mark.anyio
async def test_attach_handshake_registers_client_and_reports_attach(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    attached: list[Attach] = []
    session = SessionSocket(sock, on_attach=lambda hello, _send: attached.append(hello))
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        client = await _attach(sock, "2026-06-15-abc")
        await wait_until(lambda: session.attached_count == 1)
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
        await wait_until(lambda: session.attached_count == 2)
        session.broadcast({"type": "agent_result", "payload": {"n": 1}})
        for client in (a, b):
            buf = BufferedByteReceiveStream(client)
            line = (await buf.receive_until(b"\n", 1 << 20)).decode()
            assert parse_event(line) == {"type": "agent_result", "payload": {"n": 1}}
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
        await wait_until(lambda: session.attached_count == 1)
        await client.aclose()
        await wait_until(lambda: session.attached_count == 0)
        assert detaches == 1
        await session.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_open_gate_resolves_to_the_input_sent(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    session = SessionSocket(sock)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        client = await _attach(sock, "c1")
        await wait_until(lambda: session.attached_count == 1)
        got: list[str] = []

        async def _gate() -> None:
            got.append(await session.next_input())

        tg.start_soon(_gate)
        await anyio.sleep(0.05)
        await client.send((encode_input("ship it") + "\n").encode())
        await wait_until(lambda: got)
        assert got == ["ship it"]
        await client.aclose()
        await session.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_first_input_wins_and_loser_is_not_redelivered(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    session = SessionSocket(sock)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        a = await _attach(sock, "c1")
        b = await _attach(sock, "c2")
        await wait_until(lambda: session.attached_count == 2)
        won: list[str] = []

        async def _gate() -> None:
            won.append(await session.next_input())

        tg.start_soon(_gate)
        await anyio.sleep(0.05)
        async with anyio.create_task_group() as send_tg:
            send_tg.start_soon(a.send, (encode_input("alpha") + "\n").encode())
            send_tg.start_soon(b.send, (encode_input("beta") + "\n").encode())
        await wait_until(lambda: won)
        assert won[0] in {"alpha", "beta"}

        leftover: list[str] = []
        with anyio.move_on_after(1.0):
            leftover.append(await session.next_input())
        assert leftover == []

        await a.aclose()
        await b.aclose()
        await session.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_input_outside_a_gate_is_dropped(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    session = SessionSocket(sock)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        client = await _attach(sock, "c1")
        await wait_until(lambda: session.attached_count == 1)
        await client.send((encode_input("early") + "\n").encode())
        await anyio.sleep(0.1)

        got: list[str] = []

        async def _gate() -> None:
            got.append(await session.next_input())

        tg.start_soon(_gate)
        await anyio.sleep(0.05)
        await client.send((encode_input("now") + "\n").encode())
        await wait_until(lambda: got)
        assert got == ["now"]
        await client.aclose()
        await session.close()
        tg.cancel_scope.cancel()


def test_arm_input_latches_a_pre_gate_reply_frame() -> None:
    session = SessionSocket("/unused.sock")
    session.arm_input()
    session._dispatch(SimpleNamespace(), Input("early"))
    assert session._pending_input == "early"


def test_disarm_input_drops_an_unconsumed_latched_frame() -> None:
    session = SessionSocket("/unused.sock")
    session.arm_input()
    session._dispatch(SimpleNamespace(), Input("early"))
    session.disarm_input()
    assert session._pending_input is None
    # A frame arriving while disarmed (type-ahead during a run) is not latched,
    # so it can never leak into a later, unrelated gate.
    session._dispatch(SimpleNamespace(), Input("stale"))
    assert session._pending_input is None


@pytest.mark.anyio
async def test_next_input_delivers_a_latched_pre_gate_reply(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    session = SessionSocket(sock)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        client = await _attach(sock, "c1")
        await wait_until(lambda: session.attached_count == 1)
        session.arm_input()
        await client.send((encode_input("early") + "\n").encode())
        # The reply arrives before the gate opens; wait until it is latched.
        await wait_until(lambda: session._pending_input is not None)
        got: list[str] = []

        async def _gate() -> None:
            got.append(await session.next_input())

        tg.start_soon(_gate)
        await wait_until(lambda: got)
        assert got == ["early"]
        await client.aclose()
        await session.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_next_input_delivers_a_latched_pre_gate_skill_request(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    session = SessionSocket(sock)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        client = await _attach(sock, "c1")
        await wait_until(lambda: session.attached_count == 1)
        session.arm_input()
        await client.send(
            (encode_skill_request("refactor", "src/foo.py") + "\n").encode())
        # The skill request arrives before the gate opens; wait until it is latched.
        await wait_until(lambda: session._pending_input is not None)
        got: list[object] = []

        async def _gate() -> None:
            got.append(await session.next_input())

        tg.start_soon(_gate)
        await wait_until(lambda: got)
        assert got == [SkillRequest("refactor", "src/foo.py")]
        await client.aclose()
        await session.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_control_frame_invokes_on_control(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    actions: list[str] = []
    session = SessionSocket(sock, on_control=lambda a, _r: actions.append(a))
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        client = await _attach(sock, "c1")
        await wait_until(lambda: session.attached_count == 1)
        await client.send((encode_control("interrupt") + "\n").encode())
        await wait_until(lambda: actions)
        assert actions == ["interrupt"]
        await client.aclose()
        await session.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_open_creates_missing_parent_directory(tmp_path: Path) -> None:
    sock = str(tmp_path / "sessions" / "c1.sock")
    session = SessionSocket(sock)
    await session.open()
    assert (tmp_path / "sessions").is_dir()
    await session.close()


@pytest.mark.anyio
async def test_open_refuses_a_socket_dir_owned_by_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os as _os

    from imp.remote.session import socket_server

    sockdir = tmp_path / "imp-sock"
    sockdir.mkdir()
    session = SessionSocket(str(sockdir / "s.sock"))
    my_uid = _os.geteuid()
    monkeypatch.setattr(socket_server.os, "geteuid", lambda: my_uid + 1)
    with pytest.raises(PermissionError):
        await session.open()


@pytest.mark.anyio
async def test_open_tightens_socket_dir_with_loose_permissions(
    tmp_path: Path,
) -> None:
    sockdir = tmp_path / "imp-sock"
    sockdir.mkdir(mode=0o755)
    session = SessionSocket(str(sockdir / "s.sock"))
    await session.open()
    await session.close()
    assert sockdir.stat().st_mode & 0o777 == 0o700


def test_emit_drops_a_client_whose_bounded_buffer_overflows() -> None:
    session = SessionSocket("/unused.sock")
    send, _recv = anyio.create_memory_object_stream[dict](max_buffer_size=1)
    cancelled: list[bool] = []
    client = _Client(
        stream=SimpleNamespace(),
        send=send,
        scope=SimpleNamespace(cancel=lambda: cancelled.append(True)),
    )
    session._clients.add(client)
    send.send_nowait({"type": "first"})        # fill the buffer-1 stream
    session.broadcast({"type": "overflow"})    # second event → WouldBlock → drop
    assert client not in session._clients
    assert cancelled == [True]
    _recv.close()


async def _attach_leased(sock_path: str, quest_id: str, lease: bool) -> SocketStream:
    client = await anyio.connect_unix(sock_path)
    await client.send((encode_attach(quest_id, "T", lease) + "\n").encode())
    return client


@pytest.mark.anyio
async def test_leased_count_tracks_attach_lease_flag(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    session = SessionSocket(sock)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        leased = await _attach_leased(sock, "c1", True)
        unleased = await _attach_leased(sock, "c2", False)
        await wait_until(lambda: session.attached_count == 2)
        assert session.leased_count == 1   # only the leased attachment counts
        await leased.aclose()
        await unleased.aclose()
        await session.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_set_lease_frame_mutates_only_issuing_client(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    changes: list[bool] = []
    session = SessionSocket(sock, on_lease_change=lambda: changes.append(True))
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        a = await _attach_leased(sock, "c1", False)
        b = await _attach_leased(sock, "c2", True)
        await wait_until(lambda: session.attached_count == 2)
        assert session.leased_count == 1
        await a.send((encode_set_lease(True) + "\n").encode())    # a: False -> True
        await wait_until(lambda: session.leased_count == 2)
        await b.send((encode_set_lease(False) + "\n").encode())   # b: True -> False
        await wait_until(lambda: session.leased_count == 1)
        assert len(changes) == 2   # on_lease_change fired once per live lease mutation
        await a.aclose()
        await b.aclose()
        await session.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_open_gate_resolves_to_a_skill_request(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    session = SessionSocket(sock)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        client = await _attach(sock, "c1")
        await wait_until(lambda: session.attached_count == 1)
        got: list[object] = []

        async def _gate() -> None:
            got.append(await session.next_input())

        tg.start_soon(_gate)
        await anyio.sleep(0.05)
        await client.send(
            (encode_skill_request("refactor", "src/foo.py") + "\n").encode())
        await wait_until(lambda: got)
        assert got == [SkillRequest("refactor", "src/foo.py")]
        await client.aclose()
        await session.close()
        tg.cancel_scope.cancel()


async def _query(sock_path: str, op: str, args: dict) -> str:
    from imp.remote.session.protocol import encode_query

    client = await anyio.connect_unix(sock_path)
    try:
        await client.send((encode_query(op, args) + "\n").encode())
        chunks: list[bytes] = []
        with contextlib.suppress(anyio.EndOfStream, anyio.ClosedResourceError,
                                 anyio.BrokenResourceError):
            while True:
                chunks.append(await client.receive())
        return b"".join(chunks).decode("utf-8").rstrip("\n")
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_query_frame_is_served_without_registering_a_client(
    tmp_path: Path,
) -> None:
    from imp.remote.session.protocol import parse_result

    sock = str(tmp_path / "s.sock")

    async def on_query(op: str, args: dict) -> object:
        return {"echo": op, "args": args}

    session = SessionSocket(sock, on_query=on_query)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        line = await _query(sock, "get_events", {"quest_id": "q1"})
        assert parse_result(line) == {"echo": "get_events", "args": {"quest_id": "q1"}}
        assert session.attached_count == 0   # query never becomes an attachment
        await session.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_query_handler_exception_becomes_an_error_result(tmp_path: Path) -> None:
    from imp.remote.session.protocol import parse_result

    sock = str(tmp_path / "s.sock")

    async def on_query(op: str, args: dict) -> object:
        raise ValueError("bad op")

    session = SessionSocket(sock, on_query=on_query)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        line = await _query(sock, "nope", {})
        with pytest.raises(RuntimeError, match="bad op"):
            parse_result(line)
        await session.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_query_frame_is_dropped_when_no_on_query_handler(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")

    session = SessionSocket(sock)   # no on_query
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        reply = await _query(sock, "get_events", {"quest_id": "q1"})
        assert reply == ""                     # dropped: no result frame sent
        assert session.attached_count == 0     # never registered as a client
        await session.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_close_unlinks_socket_file(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    session = SessionSocket(sock)
    await session.open()
    assert Path(sock).exists()
    await session.close()
    assert not Path(sock).exists()


@pytest.mark.anyio
async def test_close_is_idempotent_when_socket_already_gone(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    session = SessionSocket(sock)
    await session.open()
    await session.close()
    assert not Path(sock).exists()
    # Second close (listener already None, file already gone) must not raise.
    await session.close()
    assert not Path(sock).exists()
