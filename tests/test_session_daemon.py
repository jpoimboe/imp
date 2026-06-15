from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from imp.engine.change import Change
from imp.engine.executor import RunResult
from imp.remote.session.client import SessionClient
from imp.remote.session.daemon import _is_interrupt_pause, serve_change_session
from imp.remote.session.discover import session_socket_path
from imp.remote.session.protocol import CONTROL_ABANDON


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

    with anyio.fail_after(5):
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


@pytest.mark.anyio
async def test_resident_abandon_runs_teardown(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-aba")
    sock = str(session_socket_path(change.control_root, change.id))
    torn: list[Change] = []

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
    ) -> str:
        await anyio.sleep_forever()   # only an abandon-cancel ends this run
        return "x"

    async with anyio.create_task_group() as tg:
        async def _serve() -> None:
            await serve_change_session(
                change, run_fn=fake_run, hibernate=False,
                tear_down=lambda ch: torn.append(ch))

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        client = SessionClient(sock, change.id, change.title,
                               on_event=lambda _e: None)
        await client.connect()
        await client.send_control(CONTROL_ABANDON)
        with anyio.fail_after(5):
            while not torn:
                await anyio.sleep(0.02)
        assert torn == [change]
        await client.aclose()
        tg.cancel_scope.cancel()


def test_is_interrupt_pause_true_only_for_paused_with_should_pause() -> None:
    paused_interrupt = SimpleNamespace(should_pause=True)
    paused_workflow = SimpleNamespace(should_pause=False)
    assert _is_interrupt_pause(RunResult.PAUSED, paused_interrupt) is True
    assert _is_interrupt_pause(RunResult.PAUSED, paused_workflow) is False
    assert _is_interrupt_pause(RunResult.COMPLETED, paused_interrupt) is False


@pytest.mark.anyio
async def test_interrupt_pause_reprompts_and_resumes_in_place(
    tmp_path: Path,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-rzm")
    sock = str(session_socket_path(change.control_root, change.id))
    calls = {"n": 0}

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
    ) -> RunResult:
        calls["n"] += 1
        if calls["n"] == 1:
            await coordinator.signal_pause(reason="ctrl-c")
            return RunResult.PAUSED
        assert not coordinator.should_pause  # reset() must have cleared this
        on_event({"type": "agent_output",
                  "payload": {"redirect": coordinator.drain_interject()}})
        return RunResult.COMPLETED

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
        with anyio.fail_after(5):
            while not any(
                    e.get("type") == "awaiting_user_input" for e in received):
                await anyio.sleep(0.02)
        await anyio.sleep(0.05)  # let the resume gate open (next_input is awaiting)
        await client.send_input("go left")
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] is RunResult.COMPLETED
        assert calls["n"] == 2
        assert {"type": "agent_output",
                "payload": {"redirect": "go left"}} in received
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_interrupt_pause_hibernates_with_zero_clients(
    tmp_path: Path,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-rzh")
    calls = {"n": 0}

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
    ) -> RunResult:
        calls["n"] += 1
        await coordinator.signal_pause(reason="ctrl-c")
        return RunResult.PAUSED

    with anyio.fail_after(5):
        result = await serve_change_session(
            change, run_fn=fake_run, hibernate=True)
    assert result is RunResult.PAUSED
    assert calls["n"] == 1
