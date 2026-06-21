from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import anyio
import pytest

from imp.engine.change import Change
from imp.remote.session.client import SessionClient
from imp.remote.session.reply import deliver_session_reply
from imp.remote.session.server import SessionServer
from imp.remote.session.socket_server import SessionSocket


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_deliver_session_reply_satisfies_the_daemon_gate(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    got: list[str] = []

    server = SessionServer(sock)
    await server.open()

    async def _run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        # The delivering client attaches first; only then does the daemon reach its
        # gate. Wait for the attach so the broadcast event is not missed, then mirror
        # production ordering: emit awaiting_user_input, THEN open the gate.
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "go?"}})
        reply = await user_chat_input_fn("")
        got.append(reply)
        return reply

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async with anyio.create_task_group() as tg:
        tg.start_soon(server.serve, _run_fn)
        with anyio.fail_after(5):
            await deliver_session_reply(cast(Change, None), "approved", attach=_attach)
        with anyio.fail_after(5):
            while not got:
                await anyio.sleep(0.02)
        assert got == ["approved"]
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_deliver_session_reply_satisfies_gate_when_foreground_client_already_attached(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    got: list[str] = []
    gate_emitted = anyio.Event()

    events_path = tmp_path / "events.jsonl"
    server = SessionServer(sock, events_path=events_path)
    await server.open()

    async def _run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        # Wait for the foreground client to attach before reaching the gate.
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        # Emit the gate event and open the gate — the foreground client is
        # already attached and will receive this broadcast.
        prompt_ev = {"id": "e1", "type": "awaiting_user_input", "payload": {"prompt": "go?"}}
        events_path.write_text(json.dumps(prompt_ev) + "\n")
        on_event(prompt_ev)
        # Late clients get replay (_replay=True, ignored) plus this live pending prompt.
        gate_emitted.set()
        reply = await user_chat_input_fn("")
        got.append(reply)
        return reply

    async def _attach_foreground() -> SessionClient:
        client = SessionClient(sock, "fg", "T", on_event=lambda _: None)
        await client.connect()
        return client

    async def _attach_deliver(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        # Wait until the gate event has been broadcast before connecting.
        with anyio.fail_after(5):
            await gate_emitted.wait()
        await anyio.sleep(0.01)  # realistic post-broadcast delay; event is on disk for replay
        client = SessionClient(sock, "c2", "T", on_event=on_event)
        await client.connect()
        return client

    fg_client = await _attach_foreground()
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.serve, _run_fn)
            with anyio.fail_after(5):
                await deliver_session_reply(
                    cast(Change, None), "approved", attach=_attach_deliver)
            with anyio.fail_after(5):
                while not got:
                    await anyio.sleep(0.02)
            assert got == ["approved"]
            tg.cancel_scope.cancel()
    finally:
        await fg_client.aclose()


@pytest.mark.anyio
async def test_deliver_session_reply_ignores_replayed_gate(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    socket = SessionSocket(sock)
    await socket.open()
    inputs: list[str] = []

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async with anyio.create_task_group() as tg:
        tg.start_soon(socket.serve)

        async def _server_gate() -> None:
            with anyio.fail_after(5):
                while socket.attached_count == 0:
                    await anyio.sleep(0.02)
            # A replayed (marked) gate: the delivering client must NOT answer it.
            socket.broadcast({"type": "awaiting_user_input",
                              "payload": {"prompt": "old"}, "_replay": True})
            await anyio.sleep(0.1)
            # The live (unmarked) gate, then open next_input to capture the reply.
            socket.broadcast({"type": "awaiting_user_input",
                              "payload": {"prompt": "live"}})
            inputs.append(await socket.next_input())

        tg.start_soon(_server_gate)
        with anyio.fail_after(5):
            await deliver_session_reply(cast(Change, None), "approved", attach=_attach)
        with anyio.fail_after(5):
            while not inputs:
                await anyio.sleep(0.02)
        assert inputs == ["approved"]   # answered the live gate, not the replayed one
        tg.cancel_scope.cancel()
    await socket.close()
