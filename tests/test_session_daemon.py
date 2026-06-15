from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.engine.change import Change
from imp.remote.session.client import SessionClient
from imp.remote.session.daemon import serve_change_session
from imp.remote.session.discover import session_socket_path


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_change(tmp_path: Path, change_id: str) -> Change:
    return Change(
        id=change_id,
        title="T",
        base_branch="main",
        control_root=tmp_path,
        project_root=tmp_path,
        change_dir=tmp_path / ".imp" / "change",
    )


async def _wait_for_socket(sock: str) -> None:
    with anyio.fail_after(5):
        while not Path(sock).exists():
            await anyio.sleep(0.02)


@pytest.mark.anyio
async def test_serves_on_per_change_socket_and_bridges_run_change_seams(
    tmp_path: Path,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-abc")
    sock = str(session_socket_path(change.control_root, change.id))

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
    ) -> str:
        reply = await user_chat_input_fn("")
        on_event({"type": "agent_output", "payload": {"reply": reply}})
        return reply

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_change_session(
                change, run_fn=fake_run, hibernate=False)

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        received: list[dict[str, Any]] = []
        client = SessionClient(sock, change.id, change.title,
                               on_event=received.append)
        await client.connect()
        tg.start_soon(client.receive_events)
        await anyio.sleep(0.05)  # let the gate open (next_input is awaiting)
        await client.send_input("ship it")
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "ship it"
        with anyio.fail_after(5):
            while not received:
                await anyio.sleep(0.02)
        assert received == [
            {"type": "agent_output", "payload": {"reply": "ship it"}}]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_zero_client_gate_hibernates_by_default(tmp_path: Path) -> None:
    from imp.agents.tools.wait_for_user import AwaitingUserInput

    change = _make_change(tmp_path, "2026-06-15-def")

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
    ) -> str:
        try:
            await user_chat_input_fn("")
        except AwaitingUserInput:
            return "hibernated"
        return "resident"

    result = await serve_change_session(change, run_fn=fake_run)
    assert result == "hibernated"


@pytest.mark.anyio
async def test_lock_is_held_during_run_and_released_after(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-ghi")
    lock_path = change.change_dir / "lock"
    release = anyio.Event()

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
    ) -> str:
        await release.wait()
        return "done"

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_change_session(
                change, run_fn=fake_run, hibernate=False)

        tg.start_soon(_serve)
        await anyio.sleep(0.05)  # let serve acquire the lock and enter run_fn
        assert lock_path.exists()  # ProjectLock writes the holder-pid file
        release.set()
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "done"
        assert not lock_path.exists()  # released on serve() return
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_interrupt_control_pauses_the_shared_coordinator(
    tmp_path: Path,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-jkl")
    sock = str(session_socket_path(change.control_root, change.id))

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
    ) -> str:
        with anyio.fail_after(5):
            while not coordinator.should_pause:
                await anyio.sleep(0.02)
        return "paused"

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_change_session(
                change, run_fn=fake_run, hibernate=False)

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        client = SessionClient(sock, change.id, change.title,
                               on_event=lambda _e: None)
        await client.connect()
        await client.send_control("interrupt")
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "paused"
        await client.aclose()
        tg.cancel_scope.cancel()


def test_main_loads_named_change_and_serves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import imp.remote.session.daemon as daemon

    loaded = object()
    calls: dict[str, Any] = {}

    def fake_load(control_root: Path, change_id: str) -> object:
        calls["load"] = (control_root, change_id)
        return loaded

    def fake_run(fn: Any, arg: Any) -> None:
        calls["run"] = (fn, arg)

    monkeypatch.setattr(daemon.Change, "load", staticmethod(fake_load))
    monkeypatch.setattr(daemon.anyio, "run", fake_run)

    rc = daemon.main(
        ["--control-root", str(tmp_path), "--change-id", "2026-06-15-abc"])

    assert rc == 0
    assert calls["load"] == (tmp_path, "2026-06-15-abc")
    assert calls["run"] == (daemon.serve_change_session, loaded)
