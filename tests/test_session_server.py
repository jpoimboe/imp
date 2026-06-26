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
    gate_reached = anyio.Event()

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        gate_reached.set()
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
        await gate_reached.wait()
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
    """No client attaches within first_attach_timeout -> the startup gate falls
    back to hibernation (avoids a daemon hung on the lock after a crashed client)."""
    from imp.agents.tools.wait_for_user import AwaitingUserInput

    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=True, first_attach_timeout=0.1)

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
    gate_reached = anyio.Event()

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count < 1:
                await anyio.sleep(0.02)
        gate_reached.set()
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
        await gate_reached.wait()
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
    gate_reached = anyio.Event()

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count < 1:
                await anyio.sleep(0.02)
        try:
            gate_reached.set()
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
        await gate_reached.wait()
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
    gate_reached = anyio.Event()

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count < 2:
                await anyio.sleep(0.02)
        try:
            gate_reached.set()
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
        await gate_reached.wait()
        await client1.aclose()
        await anyio.sleep(0.05)  # first detach alone must not trigger hibernation
        await client2.aclose()
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "hibernated"
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_gate_waits_for_first_attach_before_hibernating(tmp_path: Path) -> None:
    """Startup race (resume pause/exit bug): the bare-turn-end solicit reaches the
    gate before the spawning client has finished attaching. The gate must wait for
    that first attach and return its input, NOT hibernate on the transient
    zero-client startup window."""
    from imp.agents.tools.wait_for_user import AwaitingUserInput

    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=True, first_attach_timeout=5.0)
    gate_reached = anyio.Event()

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        # Enter the gate with ZERO clients attached — the daemon resumes and
        # solicits before the client's attach lands.
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
        await gate_reached.wait()
        assert server.attached_count == 0  # gate entered before any attach
        client = await _attach(sock, "c1")
        with anyio.fail_after(5):
            while server.attached_count != 1:
                await anyio.sleep(0.02)
        await client.send((encode_input("late but here") + "\n").encode())
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "late but here"
        await client.aclose()
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
async def test_journaled_gate_is_replayed_marked_plus_one_live_reemit(
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
        return await user_chat_input_fn("")   # still-open gate re-emits on late attach

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
            while len(seen) < 3:        # e0 (marked), e1 (marked), e1 (live re-emit)
                seen.append(parse_event(
                    (await buf.receive_until(b"\n", 1 << 20)).decode()))
        replayed_gates = [s for s in seen
                          if s["type"] == "awaiting_user_input" and s.get("_replay")]
        live_gates = [s for s in seen
                      if s["type"] == "awaiting_user_input" and not s.get("_replay")]
        assert replayed_gates == [{**recs[1], "_replay": True}]  # replayed once, marked
        assert live_gates == [recs[1]]                           # one live, unmarked
        await client.send((encode_input("yes") + "\n").encode())
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "yes"            # reply flows back through the open gate
        assert server._pending_prompt is None  # cleared on gate exit
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_cli_attached_emission_contract(tmp_path: Path) -> None:
    from imp.engine.state import JsonlEventLog

    events_path = tmp_path / "events.jsonl"
    sock = str(tmp_path / "s.sock")
    log = JsonlEventLog(events_path)
    release = anyio.Event()
    sentinel = {"type": "agent_output", "payload": {"text": "live"}}
    server = SessionServer(
        sock,
        events_path=events_path,
        on_first_attach=lambda: log.append(
            agent_id=None, type="cli_attached", payload={}),
    )

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count < 1:
                await anyio.sleep(0.02)
        on_event(sentinel)
        await release.wait()
        return "ok"

    def _disk_records() -> list[dict[str, Any]]:
        if not events_path.exists():
            return []
        return [json.loads(line) for line in events_path.read_text().splitlines()
                if line.strip()]

    async with anyio.create_task_group() as tg:
        await server.open()
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await server.serve(run_fn)

        tg.start_soon(_serve)

        # 0 -> 1 attach: exactly one cli_attached is journaled, never broadcast.
        client1 = await _attach(sock, "c1")
        with anyio.fail_after(5):
            while not any(r["type"] == "cli_attached" for r in _disk_records()):
                await anyio.sleep(0.02)
        attached = [r for r in _disk_records() if r["type"] == "cli_attached"]
        assert len(attached) == 1
        rec = attached[0]
        assert rec["agent_id"] is None
        assert rec["payload"] == {}
        assert rec["parent_id"] is None
        assert isinstance(rec["id"], str) and rec["id"]
        assert "timestamp" in rec

        # 1 -> 2 attach: no second cli_attached.
        client2 = await _attach(sock, "c2")
        with anyio.fail_after(5):
            while server.attached_count != 2:
                await anyio.sleep(0.02)
        await anyio.sleep(0.1)  # give any erroneous re-emit time to land
        assert sum(1 for r in _disk_records() if r["type"] == "cli_attached") == 1

        # cli_attached is journal-only: never on the first client's live stream.
        buf1 = BufferedByteReceiveStream(client1)
        seen1: list[dict[str, Any]] = []
        with anyio.move_on_after(0.5):
            while True:
                seen1.append(parse_event(
                    (await buf1.receive_until(b"\n", 1 << 20)).decode()))
        assert sentinel in seen1
        assert all(e.get("type") != "cli_attached" for e in seen1)

        release.set()
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        await client1.aclose()
        await client2.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_emit_chokepoint_registers_pending_prompt_for_late_attacher(
    tmp_path: Path,
) -> None:
    # No manual set_pending_prompt: the _emit/_chat chokepoint registers the gate.
    sock = str(tmp_path / "s.sock")
    gate_reached = anyio.Event()
    prompt_event = {"type": "awaiting_user_input", "payload": {"prompt": "go?"}}
    server = SessionServer(sock, hibernate=True)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count < 1:
                await anyio.sleep(0.02)
        on_event(prompt_event)          # _emit records the gate event
        gate_reached.set()
        return await user_chat_input_fn("")   # _chat registers it as pending

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
        assert parse_event(line) == prompt_event   # late attacher gets the live gate
        await client2.send((encode_input("yes") + "\n").encode())
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "yes"                # and can answer it
        assert server._pending_prompt is None  # cleared on gate exit
        await client1.aclose()
        await client2.aclose()
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


@pytest.mark.anyio
async def test_archive_control_cancels_run_and_sets_archived_flag(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    started = anyio.Event()
    server = SessionServer(sock)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        started.set()
        await anyio.sleep_forever()
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
        await client.send((encode_control("archive") + "\n").encode())
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert server.archived is True
        assert server.discarded is False
        assert server.saved is False
        assert server.abandoned is False
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_discard_control_cancels_run_and_sets_discarded_flag(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    started = anyio.Event()
    server = SessionServer(sock)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        started.set()
        await anyio.sleep_forever()
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
        await client.send((encode_control("discard") + "\n").encode())
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert server.discarded is True
        assert server.archived is False
        assert server.saved is False
        assert server.abandoned is False
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_archive_is_delivered_even_when_the_control_queue_is_full() -> None:
    server = SessionServer("/unused.sock")
    from imp.remote.session.server import _CONTROL_QUEUE_DEPTH
    for _ in range(_CONTROL_QUEUE_DEPTH):
        server._enqueue_control("interrupt")
    server._enqueue_control("archive")
    assert server._archive_requested is True  # signal set regardless of queue state
    async with anyio.create_task_group() as tg:
        server._serve_scope = tg.cancel_scope
        tg.start_soon(server._control_loop)
    assert server.archived is True


@pytest.mark.anyio
async def test_discard_is_delivered_even_when_the_control_queue_is_full() -> None:
    server = SessionServer("/unused.sock")
    from imp.remote.session.server import _CONTROL_QUEUE_DEPTH
    for _ in range(_CONTROL_QUEUE_DEPTH):
        server._enqueue_control("interrupt")
    server._enqueue_control("discard")
    assert server._discard_requested is True  # signal set regardless of queue state
    async with anyio.create_task_group() as tg:
        server._serve_scope = tg.cancel_scope
        tg.start_soon(server._control_loop)
    assert server.discarded is True


@pytest.mark.anyio
async def test_gate_handles_multiple_exchanges_in_one_serve(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=True)
    received: list[str] = []
    gate_reached = anyio.Event()

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count < 1:
                await anyio.sleep(0.02)
        gate_reached.set()
        received.append(await user_chat_input_fn(""))   # exchange 1
        received.append(await user_chat_input_fn(""))   # exchange 2 (follow-up)
        return "completed"                              # control returns only now

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
        await gate_reached.wait()
        await client.send((encode_input("answer one") + "\n").encode())
        with anyio.fail_after(5):
            while len(received) < 1:
                await anyio.sleep(0.02)
        # The run is blocked on the SECOND gate: serve() has NOT returned between
        # exchanges (the AC7 invariant).
        assert "r" not in result
        await client.send((encode_input("answer two") + "\n").encode())
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert received == ["answer one", "answer two"]
        assert result["r"] == "completed"   # control returns to the shell at completion
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_detach_mid_turn_pauses_then_reattach_reply_continues(
    tmp_path: Path,
) -> None:
    from imp.agents.tools.wait_for_user import AwaitingUserInput

    # Leg 1: the client detaches before the bare-turn-end gate gets an input.
    # Attendance resolves at end-of-turn -> the gate raises AwaitingUserInput and
    # the run pauses.
    sock1 = str(tmp_path / "s1.sock")
    server1 = SessionServer(sock1, hibernate=True)
    gate_reached1 = anyio.Event()

    async def run_fn1(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server1.attached_count < 1:
                await anyio.sleep(0.02)
        try:
            gate_reached1.set()
            await user_chat_input_fn("")
        except AwaitingUserInput:
            return "paused"
        return "resident"

    async with anyio.create_task_group() as tg:
        await server1.open()
        result1: dict[str, Any] = {}

        async def _serve1() -> None:
            result1["r"] = await server1.serve(run_fn1)

        tg.start_soon(_serve1)
        client1 = await _attach(sock1, "c1")
        with anyio.fail_after(5):
            while server1.attached_count != 1:
                await anyio.sleep(0.02)
        await gate_reached1.wait()
        await client1.aclose()   # detach mid-turn
        with anyio.fail_after(5):
            while "r" not in result1:
                await anyio.sleep(0.02)
        assert result1["r"] == "paused"   # detach resolved at end-of-turn -> pause
        tg.cancel_scope.cancel()

    # Leg 2: resume. A FRESH server; the human reattaches and replies, and the
    # run continues by delivering the reply through the now-open gate.
    sock2 = str(tmp_path / "s2.sock")
    server2 = SessionServer(sock2, hibernate=True)
    delivered: list[str] = []
    gate_reached2 = anyio.Event()

    async def run_fn2(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server2.attached_count < 1:
                await anyio.sleep(0.02)
        gate_reached2.set()
        reply = await user_chat_input_fn("")
        delivered.append(reply)
        return reply

    async with anyio.create_task_group() as tg:
        await server2.open()
        result2: dict[str, Any] = {}

        async def _serve2() -> None:
            result2["r"] = await server2.serve(run_fn2)

        tg.start_soon(_serve2)
        client2 = await _attach(sock2, "c1")
        with anyio.fail_after(5):
            while server2.attached_count != 1:
                await anyio.sleep(0.02)
        await gate_reached2.wait()
        await client2.send((encode_input("here is my answer") + "\n").encode())
        with anyio.fail_after(5):
            while "r" not in result2:
                await anyio.sleep(0.02)
        assert delivered == ["here is my answer"]
        assert result2["r"] == "here is my answer"   # run continued on reattach+reply
        await client2.aclose()
        tg.cancel_scope.cancel()
