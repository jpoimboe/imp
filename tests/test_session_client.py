from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.remote.session.client import SessionClient, StreamEnding
from imp.remote.session.protocol import (
    _MAX_FRAME,
    CONTROL_INTERRUPT,
    Attach,
    EndOfRun,
    SkillRequest,
    encode_end,
    encode_event,
    encode_set_lease,
)
from imp.remote.session.socket_server import SessionSocket
from tests._session_fakes import serve_raw_frames
from tests.async_wait import HANG_BACKSTOP, wait_until


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_client_attach_sends_quest_id_and_title(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    seen: list[Attach] = []
    server = SessionSocket(sock, on_attach=lambda hello, _send: seen.append(hello))
    client = SessionClient(sock, "2026-06-15-xyz", "Add dark mode",
                           on_event=lambda _e: None)
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)
        await client.connect()
        await wait_until(lambda: seen)
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
        await wait_until(lambda: server.attached_count == 1)
        server.broadcast({"type": "step_started", "payload": {"step": "plan"}})
        await wait_until(lambda: received)
        assert received == [{"type": "step_started", "payload": {"step": "plan"}}]
        await client.aclose()
        await server.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_receive_events_reports_cut_when_the_server_closes_with_no_end_frame(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionSocket(sock)
    received: list[dict[str, Any]] = []
    client = SessionClient(sock, "c1", "T", on_event=received.append)
    endings: list[StreamEnding] = []

    async def _receive() -> None:
        endings.append(await client.receive_events())

    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)
        await client.connect()
        await wait_until(lambda: server.attached_count == 1)
        tg.start_soon(_receive)
        server.broadcast({"type": "step_started", "payload": {"step": "plan"}})
        await wait_until(lambda: received)
        await server.close()
        await wait_until(lambda: endings)
        assert endings == [StreamEnding.CUT]
        # the frame delivered before the close still reached the renderer
        assert received == [{"type": "step_started", "payload": {"step": "plan"}}]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_receive_events_reports_cut_on_oversized_frame(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    received: list[dict[str, Any]] = []
    client = SessionClient(sock, "c1", "T", on_event=received.append)
    good = encode_event({"type": "step_started", "payload": {"step": "plan"}})
    async with anyio.create_task_group() as tg:
        await serve_raw_frames(
            sock, good.encode("utf-8") + b"\n" + b"x" * (_MAX_FRAME + 1), tg)
        await client.connect()
        with anyio.fail_after(HANG_BACKSTOP):
            ending = await client.receive_events()
        assert ending is StreamEnding.CUT
        # the frame that arrived before the fault still reached the renderer
        assert received == [{"type": "step_started", "payload": {"step": "plan"}}]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_receive_events_reports_cut_on_unparseable_frame(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    received: list[dict[str, Any]] = []
    client = SessionClient(sock, "c1", "T", on_event=received.append)
    good = encode_event({"type": "step_started", "payload": {"step": "plan"}})
    async with anyio.create_task_group() as tg:
        await serve_raw_frames(
            sock, good.encode("utf-8") + b"\nnot a frame\n", tg)
        await client.connect()
        with anyio.fail_after(HANG_BACKSTOP):
            ending = await client.receive_events()
        assert ending is StreamEnding.CUT
        assert received == [{"type": "step_started", "payload": {"step": "plan"}}]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_receive_events_reports_ended_on_the_end_frame(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    received: list[dict[str, Any]] = []
    client = SessionClient(sock, "c1", "T", on_event=received.append)
    good = encode_event({"type": "step_started", "payload": {"step": "plan"}})
    async with anyio.create_task_group() as tg:
        await serve_raw_frames(
            sock, (good + "\n" + encode_end() + "\n").encode("utf-8"), tg)
        await client.connect()
        with anyio.fail_after(HANG_BACKSTOP):   # deadlock backstop only
            ending = await client.receive_events()
        assert ending is StreamEnding.ENDED
        # the events ahead of the marker still reached the renderer
        assert received == [{"type": "step_started", "payload": {"step": "plan"}}]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_receive_events_hands_the_end_frame_to_on_end(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    client = SessionClient(sock, "c1", "T", on_event=lambda _e: None)
    ends: list[EndOfRun] = []
    async with anyio.create_task_group() as tg:
        await serve_raw_frames(
            sock, (encode_end(EndOfRun("save")) + "\n").encode("utf-8"), tg)
        await client.connect()
        with anyio.fail_after(HANG_BACKSTOP):   # deadlock backstop only
            ending = await client.receive_events(on_end=ends.append)
        assert ending is StreamEnding.ENDED
        assert ends == [EndOfRun("save")]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_receive_events_propagates_a_renderer_exception(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionSocket(sock)

    def _boom(_event: dict[str, Any]) -> None:
        raise ValueError("renderer boom")

    client = SessionClient(sock, "c1", "T", on_event=_boom)
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)
        await client.connect()
        await wait_until(lambda: server.attached_count == 1)
        server.broadcast({"type": "step_started", "payload": {}})
        with anyio.fail_after(HANG_BACKSTOP), pytest.raises(ValueError, match="renderer boom"):
            await client.receive_events()
        await client.aclose()
        await server.close()
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
        await wait_until(lambda: server.attached_count == 1)
        await client.aclose()
        await wait_until(lambda: server.attached_count == 0)
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
        await wait_until(lambda: server.attached_count == 1)
        got: list[str] = []

        async def _gate() -> None:
            got.append(await server.next_input())

        tg.start_soon(_gate)
        await anyio.sleep(0.05)  # let the gate open (next_input is awaiting)
        await client.send_input("ship it")
        await wait_until(lambda: got)
        assert got == ["ship it"]
        await client.aclose()
        await server.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_client_control_invokes_server_on_control(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    actions: list[str] = []
    server = SessionSocket(sock, on_control=lambda a, _r: actions.append(a))
    client = SessionClient(sock, "c1", "T", on_event=lambda _e: None)
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)
        await client.connect()
        await wait_until(lambda: server.attached_count == 1)
        await client.send_control("interrupt")
        await wait_until(lambda: actions)
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
        await wait_until(lambda: server.attached_count == 1)
        with pytest.raises(ValueError, match="unknown control action"):
            await client.send_control("explode")
        await client.aclose()
        await server.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_client_attaches_with_lease_flag(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    seen: list[Attach] = []
    server = SessionSocket(sock, on_attach=lambda hello, _send: seen.append(hello))
    client = SessionClient(sock, "c1", "T", on_event=lambda _e: None, lease=True)
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)
        await client.connect()
        await wait_until(lambda: seen)
        assert seen == [Attach("c1", "T", True)]
        await client.aclose()
        await server.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_send_set_lease_emits_set_lease_frame(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionSocket(sock)
    client = SessionClient(sock, "c1", "T", on_event=lambda _e: None)
    sent: list[bytes] = []
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)
        await client.connect()
        await wait_until(lambda: server.attached_count == 1)
        real_send = client._stream.send  # type: ignore[union-attr]

        async def _capture(data: bytes) -> None:
            sent.append(data)
            await real_send(data)

        client._stream.send = _capture  # type: ignore[method-assign, union-attr]
        await client.send_set_lease(True)
        assert sent == [(encode_set_lease(True) + "\n").encode("utf-8")]
        await server.close()
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_concurrent_send_input_and_control_do_not_collide(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    actions: list[str] = []
    inputs: list[str] = []
    server = SessionSocket(sock, on_control=lambda a, _r: actions.append(a))
    client = SessionClient(sock, "c1", "T", on_event=lambda _e: None)
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)
        await client.connect()
        await wait_until(lambda: server.attached_count == 1)

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
        await wait_until(lambda: inputs and actions)
        assert max_concurrent == 1, "lock must serialize concurrent sends"
        assert inputs == ["reply"]
        assert actions == ["interrupt"]
        await client.aclose()
        await server.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_send_skill_request_reaches_the_gate(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionSocket(sock)
    client = SessionClient(sock, "c1", "T", on_event=lambda _e: None)
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)
        await client.connect()
        await wait_until(lambda: server.attached_count == 1)
        got: list[object] = []

        async def _gate() -> None:
            got.append(await server.next_input())

        tg.start_soon(_gate)
        await anyio.sleep(0.05)
        await client.send_skill_request("refactor", "src/foo.py")
        await wait_until(lambda: got)
        assert got == [SkillRequest("refactor", "src/foo.py")]
        await client.aclose()
        await server.close()
        tg.cancel_scope.cancel()
