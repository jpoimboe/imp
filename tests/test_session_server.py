from __future__ import annotations

import contextlib
import json
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
async def test_abandon_control_cancels_run_and_records_abandonment(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    started = anyio.Event()
    abandoned_cb: list[bool] = []
    server = SessionServer(sock, on_abandon=lambda: abandoned_cb.append(True))

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        started.set()
        await anyio.sleep_forever()   # only an abandon-cancel can end this run
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
        await started.wait()
        await client.send((encode_control("abandon") + "\n").encode())
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert server.abandoned is True
        assert abandoned_cb == [True]
        assert result["r"] is None   # run cancelled mid-flight; no return value
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


@pytest.mark.anyio
async def test_lock_is_released_when_run_fn_raises(tmp_path: Path) -> None:
    from imp.engine.state import ProjectLock

    sock = str(tmp_path / "s.sock")
    lock_path = tmp_path / "lock"
    server = SessionServer(sock, lock=ProjectLock(lock_path))

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        raise RuntimeError("boom")

    await server.open()
    try:
        await server.serve(run_fn)
    except* RuntimeError:
        pass
    assert not lock_path.exists()


@pytest.mark.anyio
async def test_gate_hibernates_when_two_clients_detach(tmp_path: Path) -> None:
    from imp.agents.tools.wait_for_user import AwaitingUserInput

    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=True)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count < 2:
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
        client1 = await _attach(sock, "c1")
        client2 = await _attach(sock, "c2")
        with anyio.fail_after(5):
            while server.attached_count != 2:
                await anyio.sleep(0.02)
        await anyio.sleep(0.05)  # let the gate open while both are attached
        await client1.aclose()
        await anyio.sleep(0.05)  # first detach alone must not trigger hibernation
        await client2.aclose()
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "hibernated"
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_pending_prompt_is_redelivered_to_late_connecting_client(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    gate_reached = anyio.Event()
    prompt_event = {"type": "awaiting_user_input", "payload": {"prompt": "paused"}}
    server = SessionServer(sock, hibernate=True)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        from imp.agents.tools.wait_for_user import AwaitingUserInput

        with anyio.fail_after(5):
            while server.attached_count < 1:
                await anyio.sleep(0.02)
        server.set_pending_prompt(prompt_event)
        on_event(prompt_event)
        gate_reached.set()
        try:
            return await user_chat_input_fn("")
        except AwaitingUserInput:
            return "hibernated"

    async with anyio.create_task_group() as tg:
        await server.open()
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await server.serve(run_fn)

        tg.start_soon(_serve)
        client1 = await _attach(sock, "c1")
        await gate_reached.wait()
        client2 = await _attach(sock, "c2")
        buf2 = BufferedByteReceiveStream(client2)
        with anyio.fail_after(5):
            line = (await buf2.receive_until(b"\n", 1 << 20)).decode()
        assert parse_event(line) == prompt_event
        await client1.aclose()
        await client2.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_abandon_is_delivered_even_when_the_control_queue_is_full() -> None:
    abandoned: list[bool] = []
    server = SessionServer("/unused.sock", on_abandon=lambda: abandoned.append(True))
    from imp.remote.session.server import _CONTROL_QUEUE_DEPTH
    for _ in range(_CONTROL_QUEUE_DEPTH):
        server._enqueue_control("interrupt")
    server._enqueue_control("abandon")
    assert server._abandon_requested is True  # signal set regardless of queue state
    async with anyio.create_task_group() as tg:
        server._serve_scope = tg.cancel_scope
        tg.start_soon(server._control_loop)
        # _dispatch_control(CONTROL_ABANDON) calls on_abandon() then cancels _serve_scope,
        # which cancels the task group — the loop exits cleanly.
    assert abandoned == [True]


@pytest.mark.anyio
async def test_ordinary_control_overflow_is_logged_not_silently_suppressed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging
    server = SessionServer("/unused.sock")
    from imp.remote.session.server import _CONTROL_QUEUE_DEPTH
    for _ in range(_CONTROL_QUEUE_DEPTH):
        server._enqueue_control("interrupt")
    with caplog.at_level(logging.WARNING, logger="imp.remote"):
        server._enqueue_control("interrupt")   # overflow
    assert any("control" in r.message.lower() and "overflow" in r.message.lower()
               for r in caplog.records)


@pytest.mark.anyio
async def test_explicit_detach_control_reaches_the_detach_teardown(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    detaches: list[bool] = []
    server._socket._on_detach = lambda: detaches.append(True)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        with anyio.fail_after(5):
            while server.attached_count != 0:
                await anyio.sleep(0.02)
        return "detached"

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
        await client.send((encode_control("detach") + "\n").encode())
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "detached"
        assert detaches == [True]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_late_attach_does_not_duplicate_prompt_on_existing_client(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    gate_reached = anyio.Event()
    prompt_event = {"type": "awaiting_user_input", "payload": {"prompt": "paused"}}
    server = SessionServer(sock, hibernate=True)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        from imp.agents.tools.wait_for_user import AwaitingUserInput

        with anyio.fail_after(5):
            while server.attached_count < 1:
                await anyio.sleep(0.02)
        server.set_pending_prompt(prompt_event)
        on_event(prompt_event)
        gate_reached.set()
        try:
            return await user_chat_input_fn("")
        except AwaitingUserInput:
            return "hibernated"

    seen1: list[dict[str, Any]] = []

    async def _read(buf: BufferedByteReceiveStream, sink: list) -> None:
        with contextlib.suppress(anyio.EndOfStream, anyio.IncompleteRead,
                                 anyio.ClosedResourceError):
            while True:
                line = (await buf.receive_until(b"\n", 1 << 20)).decode()
                sink.append(parse_event(line))

    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve, run_fn)
        client1 = await _attach(sock, "c1")
        tg.start_soon(_read, BufferedByteReceiveStream(client1), seen1)
        await gate_reached.wait()
        with anyio.fail_after(5):
            while seen1.count(prompt_event) < 1:
                await anyio.sleep(0.02)
        # A second client attaches AFTER the prompt already fired.
        client2 = await _attach(sock, "c2")
        seen2: list[dict[str, Any]] = []
        tg.start_soon(_read, BufferedByteReceiveStream(client2), seen2)
        with anyio.fail_after(5):
            while seen2.count(prompt_event) < 1:
                await anyio.sleep(0.02)
        await anyio.sleep(0.1)  # give any buggy re-broadcast time to land on c1
        assert seen1.count(prompt_event) == 1  # NOT re-delivered to the first client
        assert seen2.count(prompt_event) == 1  # delivered once to the late client
        await client1.aclose()
        await client2.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_journaled_awaiting_input_is_replayed_once_not_doubled(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    sock = str(tmp_path / "s.sock")
    recs = [
        {"id": "e0", "type": "step_started", "payload": {"step": "spec"}},
        {"id": "e1", "type": "awaiting_user_input", "payload": {"prompt": "go?"}},
    ]
    events_path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    server = SessionServer(sock, events_path=events_path)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        for r in recs:                  # broadcast before attach: lost to the client
            on_event(r)
        return await user_chat_input_fn("")   # the still-open gate solicits on late attach

    async with anyio.create_task_group() as tg:
        await server.open()
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await server.serve(run_fn)

        tg.start_soon(_serve)
        client = await _attach(sock, "c1")
        buf = BufferedByteReceiveStream(client)
        seen: list[dict[str, Any]] = []
        with anyio.fail_after(5):
            while len(seen) < 2:
                seen.append(parse_event(
                    (await buf.receive_until(b"\n", 1 << 20)).decode()))
        got_extra = False
        with anyio.move_on_after(0.2):
            await buf.receive_until(b"\n", 1 << 20)
            got_extra = True
        assert not got_extra, "unexpected third event (double-delivery)"
        assert sum(1 for s in seen if s["type"] == "awaiting_user_input") == 1
        assert server._pending_prompt is None
        await client.send((encode_input("yes") + "\n").encode())
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "yes"            # reply flows back through the open gate
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_synthetic_resume_prompt_lands_once_after_replay(tmp_path: Path) -> None:
    # hibernate=False keeps the gate open so a late client can still attach and reply.
    events_path = tmp_path / "events.jsonl"
    sock = str(tmp_path / "s.sock")
    recs = [{"id": "e0", "type": "step_started", "payload": {"step": "spec"}},
            {"id": "e1", "type": "agent_output", "payload": {"text": "hi"}}]
    events_path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    synthetic = {"type": "awaiting_user_input", "payload": {"prompt": "paused"}}  # no id
    prompt_ready = anyio.Event()
    server = SessionServer(sock, events_path=events_path)   # hibernate=False (default)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        for r in recs:
            on_event(r)
        server.set_pending_prompt(synthetic)
        on_event(synthetic)            # no client yet: live broadcast lost; not on disk
        prompt_ready.set()
        return await user_chat_input_fn("")   # blocking gate keeps the server alive

    async with anyio.create_task_group() as tg:
        await server.open()
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await server.serve(run_fn)

        tg.start_soon(_serve)
        await prompt_ready.wait()
        client = await _attach(sock, "c1")
        buf = BufferedByteReceiveStream(client)
        seen: list[dict[str, Any]] = []
        with anyio.fail_after(5):
            while len(seen) < 3:
                seen.append(parse_event(
                    (await buf.receive_until(b"\n", 1 << 20)).decode()))
        assert [s.get("id") for s in seen[:2]] == ["e0", "e1"]   # replay prefix, no off-by-one
        assert seen[2] == synthetic                               # synthetic prompt after replay
        assert sum(1 for s in seen if s.get("type") == "awaiting_user_input") == 1
        await client.send((encode_input("done") + "\n").encode())   # unblock the gate
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "done"           # reply flows back through the blocking gate
        await client.aclose()
        tg.cancel_scope.cancel()
