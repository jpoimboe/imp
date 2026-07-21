from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from anyio.abc import SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream

from imp.agents.skills import SkillNotFound
from imp.agents.transcript import SkillReply
from imp.remote.session.protocol import (
    encode_attach,
    encode_control,
    encode_input,
    encode_skill_request,
    parse_event,
)
from imp.remote.session.server import SessionServer
from tests.async_wait import wait_until


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _attach(sock_path: str, quest_id: str) -> SocketStream:
    client = await anyio.connect_unix(sock_path)
    await client.send((encode_attach(quest_id, "T", True) + "\n").encode())
    return client


class _RecordingLog:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def append(self, *, agent_id: str | None, type: str,
               payload: dict[str, Any]) -> dict[str, Any]:
        rec = {"agent_id": agent_id, "type": type, "payload": payload}
        self.records.append(rec)
        return rec


@pytest.mark.anyio
async def test_run_fn_events_are_broadcast_to_attached_client(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    release = anyio.Event()
    server = SessionServer(sock)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count >= 1)
        on_event({"type": "agent_result", "payload": {"text": "hi"}})
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
            "type": "agent_result", "payload": {"text": "hi"}}
        release.set()
        await wait_until(lambda: "r" in result)
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
        await wait_until(lambda: server.attached_count == 1)
        await gate_reached.wait()
        await client.send((encode_input("ship it") + "\n").encode())
        await wait_until(lambda: "r" in result)
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
        await wait_until(lambda: server.attached_count == 1)
        assert not coord.should_pause
        await client.send((encode_control("interrupt") + "\n").encode())
        await wait_until(lambda: coord.should_pause)
        assert coord.should_pause
        release.set()
        await wait_until(lambda: "r" in result)
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
        await wait_until(lambda: server.attached_count == 1)
        await started.wait()
        await client.send((encode_control("abandon") + "\n").encode())
        await wait_until(lambda: "r" in result)
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
        await wait_until(lambda: "r" in result)
        assert result["r"] == "done"
        assert not lock_path.exists()  # released on serve() return
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_gate_with_zero_clients_hibernates(tmp_path: Path) -> None:
    """No client attaches within first_attach_timeout -> the startup gate falls
    back to hibernation (avoids a daemon hung on the lock after a crashed client)."""
    from imp.engine.awaiting_user_input import AwaitingUserInput

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
        await wait_until(lambda: "r" in result)
        assert result["r"] == "hibernated"
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_gate_with_attached_client_returns_first_input(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=True)
    received: list[str] = []
    gate_reached = anyio.Event()

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count >= 1)
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
        await wait_until(lambda: server.attached_count == 1)
        await gate_reached.wait()
        await client.send((encode_input("ship it") + "\n").encode())
        await wait_until(lambda: "r" in result)
        assert result["r"] == "ship it"
        assert received == ["ship it"]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_gate_hibernates_when_last_client_detaches(tmp_path: Path) -> None:
    from imp.engine.awaiting_user_input import AwaitingUserInput

    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=True)
    gate_reached = anyio.Event()

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count >= 1)
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
        await wait_until(lambda: server.attached_count == 1)
        await gate_reached.wait()
        await client.aclose()
        await wait_until(lambda: "r" in result)
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
    from imp.engine.awaiting_user_input import AwaitingUserInput

    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=True)
    gate_reached = anyio.Event()

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count >= 2)
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
        await wait_until(lambda: server.attached_count == 2)
        await gate_reached.wait()
        await client1.aclose()
        await anyio.sleep(0.05)  # first detach alone must not trigger hibernation
        await client2.aclose()
        await wait_until(lambda: "r" in result)
        assert result["r"] == "hibernated"
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_gate_waits_for_first_attach_before_hibernating(tmp_path: Path) -> None:
    """Startup race (resume pause/exit bug): the bare-turn-end solicit reaches the
    gate before the spawning client has finished attaching. The gate must wait for
    that first attach and return its input, NOT hibernate on the transient
    zero-client startup window."""
    from imp.engine.awaiting_user_input import AwaitingUserInput

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
        await wait_until(lambda: server.attached_count == 1)
        await client.send((encode_input("late but here") + "\n").encode())
        await wait_until(lambda: "r" in result)
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
        from imp.engine.awaiting_user_input import AwaitingUserInput

        await wait_until(lambda: server.attached_count >= 1)
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
        with anyio.fail_after(30):  # deadlock backstop only
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
        await wait_until(lambda: server.attached_count != 0)
        await wait_until(lambda: server.attached_count == 0)
        return "detached"

    async with anyio.create_task_group() as tg:
        await server.open()
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await server.serve(run_fn)

        tg.start_soon(_serve)
        client = await _attach(sock, "c1")
        await wait_until(lambda: server.attached_count == 1)
        await client.send((encode_control("detach") + "\n").encode())
        await wait_until(lambda: "r" in result)
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
        from imp.engine.awaiting_user_input import AwaitingUserInput

        await wait_until(lambda: server.attached_count >= 1)
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
        await wait_until(lambda: seen1.count(prompt_event) >= 1)
        # A second client attaches AFTER the prompt already fired.
        client2 = await _attach(sock, "c2")
        seen2: list[dict[str, Any]] = []
        tg.start_soon(_read, BufferedByteReceiveStream(client2), seen2)
        await wait_until(lambda: seen2.count(prompt_event) >= 1)
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
        with anyio.fail_after(30):  # deadlock backstop only
            while len(seen) < 4:  # e0 (marked), e1 (marked), boundary, e1 (live re-emit)
                seen.append(parse_event(
                    (await buf.receive_until(b"\n", 1 << 20)).decode()))
        assert seen[2] == {"type": "replay_complete"}            # boundary closes the replay
        replayed_gates = [s for s in seen
                          if s["type"] == "awaiting_user_input" and s.get("_replay")]
        live_gates = [s for s in seen
                      if s["type"] == "awaiting_user_input" and not s.get("_replay")]
        assert replayed_gates == [{**recs[1], "_replay": True}]  # replayed once, marked
        assert live_gates == [recs[1]]                           # one live, unmarked
        await client.send((encode_input("yes") + "\n").encode())
        await wait_until(lambda: "r" in result)
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
    sentinel = {"type": "agent_result", "payload": {"text": "live"}}
    server = SessionServer(
        sock,
        events_path=events_path,
        on_first_attach=lambda: log.append(
            agent_id=None, type="cli_attached", payload={}),
    )

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count >= 1)
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
        await wait_until(lambda: any(r["type"] == "cli_attached" for r in _disk_records()))
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
        await wait_until(lambda: server.attached_count == 2)
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
        await wait_until(lambda: "r" in result)
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
        await wait_until(lambda: server.attached_count >= 1)
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
        with anyio.fail_after(30):  # deadlock backstop only
            line = (await buf2.receive_until(b"\n", 1 << 20)).decode()
        assert parse_event(line) == prompt_event   # late attacher gets the live gate
        await client2.send((encode_input("yes") + "\n").encode())
        await wait_until(lambda: "r" in result)
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
            {"id": "e1", "type": "agent_result", "payload": {"text": "hi"}}]
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
        with anyio.fail_after(30):  # deadlock backstop only
            while len(seen) < 4:
                seen.append(parse_event(
                    (await buf.receive_until(b"\n", 1 << 20)).decode()))
        assert [s.get("id") for s in seen[:2]] == ["e0", "e1"]   # replay prefix, no off-by-one
        assert seen[2] == {"type": "replay_complete"}            # boundary closes the replay
        assert seen[3] == synthetic                               # synthetic prompt after boundary
        assert sum(1 for s in seen if s.get("type") == "awaiting_user_input") == 1
        await client.send((encode_input("done") + "\n").encode())   # unblock the gate
        await wait_until(lambda: "r" in result)
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
        await wait_until(lambda: server.attached_count == 1)
        await started.wait()
        await client.send((encode_control("archive") + "\n").encode())
        await wait_until(lambda: "r" in result)
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
        await wait_until(lambda: server.attached_count == 1)
        await started.wait()
        await client.send((encode_control("discard") + "\n").encode())
        await wait_until(lambda: "r" in result)
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
        await wait_until(lambda: server.attached_count >= 1)
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
        await wait_until(lambda: server.attached_count == 1)
        await gate_reached.wait()
        await client.send((encode_input("answer one") + "\n").encode())
        await wait_until(lambda: len(received) >= 1)
        # The run is blocked on the SECOND gate: serve() has NOT returned between
        # exchanges (the AC7 invariant).
        assert "r" not in result
        await client.send((encode_input("answer two") + "\n").encode())
        await wait_until(lambda: "r" in result)
        assert received == ["answer one", "answer two"]
        assert result["r"] == "completed"   # control returns to the shell at completion
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_detach_mid_turn_pauses_then_reattach_reply_continues(
    tmp_path: Path,
) -> None:
    from imp.engine.awaiting_user_input import AwaitingUserInput

    # Leg 1: the client detaches before the bare-turn-end gate gets an input.
    # Attendance resolves at end-of-turn -> the gate raises AwaitingUserInput and
    # the run pauses.
    sock1 = str(tmp_path / "s1.sock")
    server1 = SessionServer(sock1, hibernate=True)
    gate_reached1 = anyio.Event()

    async def run_fn1(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server1.attached_count >= 1)
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
        await wait_until(lambda: server1.attached_count == 1)
        await gate_reached1.wait()
        await client1.aclose()   # detach mid-turn
        await wait_until(lambda: "r" in result1)
        assert result1["r"] == "paused"   # detach resolved at end-of-turn -> pause
        tg.cancel_scope.cancel()

    # Leg 2: resume. A FRESH server; the human reattaches and replies, and the
    # run continues by delivering the reply through the now-open gate.
    sock2 = str(tmp_path / "s2.sock")
    server2 = SessionServer(sock2, hibernate=True)
    delivered: list[str] = []
    gate_reached2 = anyio.Event()

    async def run_fn2(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server2.attached_count >= 1)
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
        await wait_until(lambda: server2.attached_count == 1)
        await gate_reached2.wait()
        await client2.send((encode_input("here is my answer") + "\n").encode())
        await wait_until(lambda: "r" in result2)
        assert delivered == ["here is my answer"]
        assert result2["r"] == "here is my answer"   # run continued on reattach+reply
        await client2.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_lease_less_client_does_not_satisfy_keep_alive_gate(
    tmp_path: Path,
) -> None:
    from imp.engine.awaiting_user_input import AwaitingUserInput

    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=True, first_attach_timeout=0.2)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
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
        client = await anyio.connect_unix(sock)
        await client.send((encode_attach("c1", "T", False) + "\n").encode())  # lease-less
        await wait_until(lambda: server.attached_count == 1)
        # No leased attachment ever arrives: the gate hibernates on the
        # first-leased-attach timeout despite a live lease-less attachment.
        await wait_until(lambda: "r" in result)
        assert result["r"] == "hibernated"
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_last_leased_detach_pauses_mid_step(tmp_path: Path) -> None:
    from imp.cli.signals import InterruptCoordinator

    sock = str(tmp_path / "s.sock")
    coord = InterruptCoordinator()
    release = anyio.Event()
    server = SessionServer(sock, coordinator=coord, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await release.wait()   # mid-step: no gate is open
        return "done"

    async with anyio.create_task_group() as tg:
        await server.open()
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await server.serve(run_fn)

        tg.start_soon(_serve)
        client = await anyio.connect_unix(sock)
        await client.send((encode_attach("c1", "T", True) + "\n").encode())  # leased
        await wait_until(lambda: server.leased_count == 1)
        assert not coord.should_pause
        await client.aclose()   # last leased client detaches mid-step
        await wait_until(lambda: coord.should_pause)
        assert coord.should_pause
        assert coord.pause_reason == "detached"
        release.set()
        await wait_until(lambda: "r" in result)
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_lease_less_detach_does_not_pause(tmp_path: Path) -> None:
    from imp.cli.signals import InterruptCoordinator

    sock = str(tmp_path / "s.sock")
    coord = InterruptCoordinator()
    release = anyio.Event()
    server = SessionServer(sock, coordinator=coord, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await release.wait()
        return "done"

    async with anyio.create_task_group() as tg:
        await server.open()
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await server.serve(run_fn)

        tg.start_soon(_serve)
        client = await anyio.connect_unix(sock)
        await client.send((encode_attach("c1", "T", False) + "\n").encode())  # lease-less
        await wait_until(lambda: server.attached_count == 1)
        await client.aclose()
        await anyio.sleep(0.2)
        assert not coord.should_pause   # never leased -> detach is not a keep-alive loss
        release.set()
        await wait_until(lambda: "r" in result)
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_no_pause_enqueued_during_wait_for_turn(tmp_path: Path) -> None:
    # A last-leased detach while parked in the between-turns wait is resolved by
    # wait_for_turn itself (its own hibernate -> AwaitingUserInput); the detach
    # pulse must NOT also enqueue a coordinator pause (which would race a
    # conflicting pause-reason).
    from imp.cli.signals import InterruptCoordinator

    sock = str(tmp_path / "s.sock")
    coord = InterruptCoordinator()
    server = SessionServer(sock, coordinator=coord)
    server._first_leased_attach_fired = True   # a keep-alive client attached + left
    server._in_wait_for_turn = True            # parked in the between-turns wait
    server._on_detach_pulse()                  # leased_count == 0 (no socket clients)
    with pytest.raises(anyio.WouldBlock):
        server._control_recv.receive_nowait()  # nothing was enqueued


@pytest.mark.anyio
async def test_no_pause_enqueued_while_solicit_gate_open(tmp_path: Path) -> None:
    # A last-leased detach while a solicit gate is open (_pending_prompt set) is
    # resolved by the gate itself (_wait_until_unleased -> AwaitingUserInput); the
    # detach pulse must NOT also enqueue a coordinator pause.
    from imp.cli.signals import InterruptCoordinator

    sock = str(tmp_path / "s.sock")
    coord = InterruptCoordinator()
    server = SessionServer(sock, coordinator=coord)
    server._first_leased_attach_fired = True   # a keep-alive client attached + left
    server.set_pending_prompt({"type": "awaiting_user_input"})  # solicit gate open
    server._on_detach_pulse()                  # leased_count == 0 (no socket clients)
    with pytest.raises(anyio.WouldBlock):
        server._control_recv.receive_nowait()  # nothing was enqueued


@pytest.mark.anyio
async def test_lease_less_input_at_hibernating_gate_is_delivered_not_dropped(
    tmp_path: Path,
) -> None:
    # The remote deliver-reply path: deliver_session_reply attaches LEASE-LESS and
    # the rehydrate daemon runs hibernate=True. Leases gate KEEP-ALIVE, never the
    # input channel — so the gate must still open next_input() and consume the
    # injected reply rather than routing to hibernation and silently dropping it.
    from imp.engine.awaiting_user_input import AwaitingUserInput

    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=True, first_attach_timeout=2.0)
    gate_reached = anyio.Event()

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count >= 1)
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
        client = await anyio.connect_unix(sock)
        await client.send((encode_attach("c1", "T", False) + "\n").encode())  # lease-less reply
        await gate_reached.wait()
        await client.send((encode_input("the reply") + "\n").encode())
        await wait_until(lambda: "r" in result)
        assert result["r"] == "the reply"   # consumed, NOT dropped to hibernation
        await client.aclose()
        tg.cancel_scope.cancel()


def _spy_emit(server: SessionServer) -> list[dict[str, Any]]:
    """Record every event the server broadcasts, still forwarding each."""
    emitted: list[dict[str, Any]] = []
    orig = server._emit

    def _wrapper(event: dict[str, Any]) -> None:
        emitted.append(event)
        orig(event)

    server._emit = _wrapper  # type: ignore[method-assign]
    return emitted


@pytest.mark.anyio
async def test_chat_broadcasts_user_input_resolved_after_gate(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock)
    emitted = _spy_emit(server)
    gate_reached = anyio.Event()

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        gate_reached.set()
        return await user_chat_input_fn("")

    async with anyio.create_task_group() as tg:
        await server.open()
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await server.serve(run_fn)

        tg.start_soon(_serve)
        client = await _attach(sock, "c1")
        await wait_until(lambda: server.attached_count == 1)
        await gate_reached.wait()
        await client.send((encode_input("ship it") + "\n").encode())
        await wait_until(lambda: "r" in result)
        assert result["r"] == "ship it"
        resolved = [e for e in emitted if e.get("type") == "user_input_resolved"]
        assert resolved == [{"type": "user_input_resolved", "payload": {}}]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_hibernation_gate_with_input_broadcasts_resolved(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=True)
    emitted = _spy_emit(server)
    gate_reached = anyio.Event()

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count >= 1)
        gate_reached.set()
        return await user_chat_input_fn("")

    async with anyio.create_task_group() as tg:
        await server.open()
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await server.serve(run_fn)

        tg.start_soon(_serve)
        client = await _attach(sock, "c1")
        await wait_until(lambda: server.attached_count == 1)
        await gate_reached.wait()
        await client.send((encode_input("ship it") + "\n").encode())
        await wait_until(lambda: "r" in result)
        assert result["r"] == "ship it"
        resolved = [e for e in emitted if e.get("type") == "user_input_resolved"]
        assert resolved == [{"type": "user_input_resolved", "payload": {}}]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_hibernation_gate_without_input_does_not_broadcast_resolved(
    tmp_path: Path,
) -> None:
    from imp.engine.awaiting_user_input import AwaitingUserInput

    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=True)
    emitted = _spy_emit(server)
    gate_reached = anyio.Event()

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count >= 1)
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
        await wait_until(lambda: server.attached_count == 1)
        await gate_reached.wait()
        await client.aclose()
        await wait_until(lambda: "r" in result)
        assert result["r"] == "hibernated"
        assert not any(e.get("type") == "user_input_resolved" for e in emitted)
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_dispatch_control_tags_frame_and_does_not_block() -> None:
    import anyio

    from imp.cli.signals import InterruptCoordinator
    from imp.remote.session.protocol import CONTROL_DETACH, CONTROL_INTERRUPT
    from imp.remote.session.server import SessionServer
    from tests.test_interrupt_events import RecordingLog

    coord = InterruptCoordinator()
    log = RecordingLog()
    coord.set_event_log(log)
    started = anyio.Event()
    release = anyio.Event()

    async def hanging_interrupt() -> None:
        started.set()
        await release.wait()

    coord.attach_runner(hanging_interrupt)
    server = SessionServer("/tmp/does-not-matter.sock", coordinator=coord)

    detached = {"hit": False}

    async def fake_detach_all() -> None:
        detached["hit"] = True

    server._socket.detach_all = fake_detach_all  # type: ignore[assignment]

    async with anyio.create_task_group() as tg:
        coord.set_delivery_spawner(tg.start_soon)
        with anyio.fail_after(30):  # deadlock backstop only
            # First frame: interrupt delivery hangs, but dispatch returns at once.
            await server._dispatch_control(CONTROL_INTERRUPT)
            # Second frame dispatches despite the still-hanging delivery.
            await server._dispatch_control(CONTROL_DETACH)
        assert detached["hit"] is True  # non-blocking: the second frame ran
        # The daemon control frame is journaled as via="frame" (not "signal").
        reqs = [r["payload"] for r in log.records if r["type"] == "interrupt_requested"]
        assert reqs == [{"source": "ctrl-c", "via": "frame"}]
        await started.wait()
        release.set()       # let the spawned delivery task finish
        coord.note_yield()  # disarm the spawned yield watchdog so the group closes


@pytest.mark.anyio
async def test_dispatch_control_force_skips_cooperative_and_hard_kills() -> None:
    import anyio

    from imp.cli.signals import InterruptCoordinator
    from imp.remote.session.protocol import CONTROL_INTERRUPT_FORCE
    from imp.remote.session.server import SessionServer
    from tests.test_interrupt_events import RecordingLog

    coord = InterruptCoordinator()
    log = RecordingLog()
    coord.set_event_log(log)

    coop = {"n": 0}

    async def cooperative() -> None:
        coop["n"] += 1

    killed = {"n": 0}

    async def fk() -> None:
        killed["n"] += 1

    server = SessionServer("/tmp/does-not-matter.sock", coordinator=coord)
    with coord.runner_attached(cooperative, force_kill=fk, agent_id="a1"):
        with anyio.fail_after(30):  # deadlock backstop only
            await server._dispatch_control(CONTROL_INTERRUPT_FORCE)

    assert coop["n"] == 0        # cooperative tier skipped
    assert killed["n"] == 1      # hard kill fired
    esc = [r["payload"] for r in log.records if r["type"] == "interrupt_escalated"]
    assert esc == [{"reason": "second_gesture", "killed": ["a1"]}]


@pytest.mark.anyio
async def test_save_reason_survives_a_full_control_queue() -> None:
    from imp.remote.session.protocol import CONTROL_INTERRUPT, CONTROL_SAVE
    from imp.remote.session.server import _CONTROL_QUEUE_DEPTH

    server = SessionServer("/unused.sock")
    # Fill the queue with reason-less frames so the save frame's own enqueue is dropped.
    for _ in range(_CONTROL_QUEUE_DEPTH):
        server._enqueue_control(CONTROL_INTERRUPT)
    server._enqueue_control(CONTROL_SAVE, "ctrl-d")
    assert server._save_requested is True   # flag set regardless of queue state

    async with anyio.create_task_group() as tg:
        server._serve_scope = tg.cancel_scope
        tg.start_soon(server._control_loop)
        # _dispatch_control(CONTROL_SAVE, ...) flips self.saved and cancels _serve_scope,
        # which cancels the task group — the loop exits cleanly.
    assert server.saved is True
    assert server.save_reason == "ctrl-d"   # the stored reason, not a buffered frame's None


@pytest.mark.anyio
async def test_chat_loads_skill_and_returns_skill_reply(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(
        sock, hibernate=False, skill_loader=lambda name: "BODY")
    captured: list[object] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> object:
        await wait_until(lambda: server.attached_count >= 1)
        on_event({"type": "awaiting_user_input", "payload": {"prompt": ""}})
        reply = await user_chat_input_fn("")
        captured.append(reply)
        return reply

    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve, run_fn)
        client = await _attach(sock, "c1")
        await wait_until(lambda: server.attached_count >= 1)
        await anyio.sleep(0.05)
        await client.send(
            (encode_skill_request("refactor", "src/foo.py") + "\n").encode())
        await wait_until(lambda: captured)
        assert captured == [SkillReply(
            command="/refactor src/foo.py", name="refactor",
            body="BODY\n\nsrc/foo.py\n")]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_chat_skill_load_failure_degrades_to_verbatim_input(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")

    def _boom(name: str) -> str:
        raise SkillNotFound(f"no such skill {name!r}")

    server = SessionServer(sock, hibernate=False, skill_loader=_boom)
    captured: list[object] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> object:
        await wait_until(lambda: server.attached_count >= 1)
        on_event({"type": "awaiting_user_input", "payload": {"prompt": ""}})
        reply = await user_chat_input_fn("")
        captured.append(reply)
        return reply

    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve, run_fn)
        client = await _attach(sock, "c1")
        await wait_until(lambda: server.attached_count >= 1)
        await anyio.sleep(0.05)
        await client.send(
            (encode_skill_request("refactor", "src/foo.py") + "\n").encode())
        await wait_until(lambda: captured)
        assert captured == ["/refactor src/foo.py"]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_chat_skill_without_args_returns_body_unchanged(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(
        sock, hibernate=False, skill_loader=lambda name: "BODY")
    captured: list[object] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> object:
        await wait_until(lambda: server.attached_count >= 1)
        on_event({"type": "awaiting_user_input", "payload": {"prompt": ""}})
        captured.append(await user_chat_input_fn(""))
        return None

    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve, run_fn)
        client = await _attach(sock, "c1")
        await wait_until(lambda: server.attached_count >= 1)
        await anyio.sleep(0.05)
        await client.send(
            (encode_skill_request("debug", "") + "\n").encode())
        await wait_until(lambda: captured)
        assert captured == [SkillReply(
            command="/debug", name="debug", body="BODY")]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_model_override_frame_unwinds_gate_without_detach(tmp_path: Path) -> None:
    from imp.cli.signals import InterruptCoordinator
    from imp.engine.awaiting_user_input import AwaitingUserInput

    sock = str(tmp_path / "s.sock")
    coord = InterruptCoordinator()
    server = SessionServer(sock, coordinator=coord, hibernate=True)
    gate_reached = anyio.Event()
    captured: dict[str, Any] = {}

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.leased_count >= 1)
        gate_reached.set()
        try:
            await user_chat_input_fn("")
        except AwaitingUserInput:
            captured["leased_at_pause"] = server.leased_count
            return "paused"
        return "resident"

    async with anyio.create_task_group() as tg:
        await server.open()
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await server.serve(run_fn)

        tg.start_soon(_serve)
        client = await _attach(sock, "c1")
        await wait_until(lambda: server.leased_count == 1)
        await gate_reached.wait()
        await client.send((encode_control("set-model", "opus") + "\n").encode())
        await wait_until(lambda: "r" in result)
        assert result["r"] == "paused"
        assert captured["leased_at_pause"] == 1     # still leased: no detach
        assert coord.pause_reason == "model-override"
        assert coord.take_pending_model() == "opus"
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_model_override_frame_no_interrupt_or_kill_events(tmp_path: Path) -> None:
    from imp.cli.signals import InterruptCoordinator
    from imp.engine.awaiting_user_input import AwaitingUserInput

    sock = str(tmp_path / "s.sock")
    coord = InterruptCoordinator()
    log = _RecordingLog()
    coord.set_event_log(log)
    server = SessionServer(sock, coordinator=coord, hibernate=True)
    gate_reached = anyio.Event()
    captured: dict[str, Any] = {}
    interrupt_calls: list[str] = []

    async def fake_runner_interrupt() -> None:
        interrupt_calls.append("interrupt")

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.leased_count >= 1)
        coord.attach_runner(fake_runner_interrupt)   # a runner IS attached at the prompt
        gate_reached.set()
        try:
            await user_chat_input_fn("")
        except AwaitingUserInput:
            captured["leased_at_pause"] = server.leased_count
            return "paused"
        return "resident"

    async with anyio.create_task_group() as tg:
        await server.open()
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await server.serve(run_fn)

        tg.start_soon(_serve)
        client = await _attach(sock, "c1")
        await wait_until(lambda: server.leased_count == 1)
        await gate_reached.wait()
        await client.send((encode_control("set-model", "opus") + "\n").encode())
        await wait_until(lambda: "r" in result)
        assert result["r"] == "paused"
        assert captured["leased_at_pause"] == 1
        assert interrupt_calls == []                # runner never interrupted
        types = {r["type"] for r in log.records}
        assert "interrupt_requested" in types
        assert "interrupt_delivered" not in types   # no cooperative/hard kill
        assert "interrupt_escalated" not in types
        assert "conversation_archived" not in types
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_wait_for_side_effect_returns_when_pause_already_committed(
    tmp_path: Path,
) -> None:
    from imp.cli.signals import InterruptCoordinator

    sock = str(tmp_path / "s.sock")
    coord = InterruptCoordinator()
    server = SessionServer(sock, coordinator=coord, hibernate=True)
    await server.open()

    # Commit the side-effect pause state BEFORE the wait — mirrors a set-model
    # frame processed before the gate is entered. No pulse happens after this
    # point, so the _side_effect_pending event stays unset.
    coord.set_pending_model("opus")
    await coord.signal_pause(reason="model-override", via="frame")

    # Level-triggered: must return on the first predicate check, not hang on
    # evt.wait(). A regression to await-only would time out here.
    with anyio.fail_after(30):  # deadlock backstop only
        await server._wait_for_side_effect()


@pytest.mark.anyio
async def test_set_sandbox_control_flips_override_no_pause(tmp_path: Path) -> None:
    from imp.cli.signals import InterruptCoordinator
    from imp.remote.session.protocol import CONTROL_SET_SANDBOX

    coord = InterruptCoordinator()
    server = SessionServer(str(tmp_path / "s.sock"), coordinator=coord)
    assert coord.unsandboxed is False

    await server._dispatch_control(CONTROL_SET_SANDBOX, "off")
    assert coord.unsandboxed is True
    assert coord.should_pause is False   # no signal_pause — live flag only

    await server._dispatch_control(CONTROL_SET_SANDBOX, "on")
    assert coord.unsandboxed is False
    assert coord.should_pause is False


@pytest.mark.anyio
async def test_set_sandbox_control_broadcasts_transient_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.cli.signals import InterruptCoordinator
    from imp.remote.session.protocol import CONTROL_SET_SANDBOX

    coord = InterruptCoordinator()
    server = SessionServer(str(tmp_path / "s.sock"), coordinator=coord)
    broadcasts: list[dict[str, Any]] = []
    # Capture at broadcast to prove the marker rides the broadcast-only channel, not events.jsonl.
    monkeypatch.setattr(server._socket, "broadcast", broadcasts.append)

    await server._dispatch_control(CONTROL_SET_SANDBOX, "off")
    await server._dispatch_control(CONTROL_SET_SANDBOX, "on")

    markers = [e for e in broadcasts if e.get("type") == "sandbox_marker"]
    assert [m["payload"]["unsandboxed"] for m in markers] == [True, False]


def test_sandbox_toggle_confirmations_verbatim() -> None:
    from imp.remote.session import server
    assert server.SANDBOX_OFF_CONFIRMED == (
        "Sandbox off — this step is now unsandboxed. It re-enables when the "
        "step finishes; run /sandbox on to re-enable it now.")
    assert server.SANDBOX_ON_FROM_OFF == (
        "Sandbox on. Shell and file tools are confined to the worktree again. "
        "Processes already started while unsandboxed keep their current access.")


def test_attach_pulse_seeds_marker_when_unsandboxed(tmp_path: Path) -> None:
    from imp.cli.signals import InterruptCoordinator

    coord = InterruptCoordinator()
    coord.set_unsandboxed(True)
    server = SessionServer(str(tmp_path / "s.sock"), coordinator=coord)

    sent: list[dict[str, Any]] = []
    server._on_attach_pulse(object(), sent.append)

    assert {"type": "sandbox_marker",
            "payload": {"unsandboxed": True}} in sent


def test_attach_pulse_does_not_seed_marker_when_sandboxed(tmp_path: Path) -> None:
    from imp.cli.signals import InterruptCoordinator

    coord = InterruptCoordinator()   # sandboxed: override not set
    server = SessionServer(str(tmp_path / "s.sock"), coordinator=coord)

    sent: list[dict[str, Any]] = []
    server._on_attach_pulse(object(), sent.append)

    assert not any(e.get("type") == "sandbox_marker" for e in sent)


@pytest.mark.anyio
async def test_dispatch_compact_signals_side_effect_pause(tmp_path: Path) -> None:
    from imp.cli.signals import InterruptCoordinator
    from imp.remote.session.protocol import CONTROL_COMPACT

    coord = InterruptCoordinator()
    server = SessionServer(str(tmp_path / "s.sock"), coordinator=coord)
    await server.open()
    pending = server._side_effect_pending      # snapshot the event the pulse must set
    await server._dispatch_control(CONTROL_COMPACT)
    assert coord.should_pause
    assert coord.pause_reason == "compact"
    assert pending is not None and pending.is_set()   # the side-effect wake pulse fired


@pytest.mark.anyio
async def test_retry_now_control_latches_when_backoff_armed(tmp_path: Path) -> None:
    from imp.cli.signals import InterruptCoordinator
    from imp.remote.session.protocol import CONTROL_RETRY_NOW

    coord = InterruptCoordinator()
    server = SessionServer(str(tmp_path / "s.sock"), coordinator=coord)
    coord.begin_backoff_wait()   # the daemon's backoff loop armed the wait
    await server._dispatch_control(CONTROL_RETRY_NOW)
    assert coord.retry_now_requested is True


@pytest.mark.anyio
async def test_retry_now_control_is_noop_when_no_backoff_armed(tmp_path: Path) -> None:
    from imp.cli.signals import InterruptCoordinator
    from imp.remote.session.protocol import CONTROL_RETRY_NOW

    coord = InterruptCoordinator()
    server = SessionServer(str(tmp_path / "s.sock"), coordinator=coord)
    # No backoff wait armed: the daemon's arm-gate leaves a stray `r` a no-op.
    await server._dispatch_control(CONTROL_RETRY_NOW)
    assert coord.retry_now_requested is False


def test_request_save_sets_save_flags(tmp_path: Path) -> None:
    server = SessionServer(str(tmp_path / "s.sock"))
    server.request_save("save_and_exit")
    assert server._save_requested is True
    assert server._save_reason == "save_and_exit"


def test_server_registers_save_hook_on_coordinator(tmp_path: Path) -> None:
    from imp.cli.signals import InterruptCoordinator

    coord = InterruptCoordinator()
    server = SessionServer(str(tmp_path / "s.sock"), coordinator=coord)
    coord.request_save(reason="save_and_exit")
    assert server._save_requested is True
    assert server._save_reason == "save_and_exit"
