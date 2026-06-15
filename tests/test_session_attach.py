from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.engine.change import Change
from imp.remote.session.attach import attach_or_spawn, spawn_session_daemon
from imp.remote.session.discover import session_socket_path
from imp.remote.session.socket_server import SessionSocket


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


def test_spawn_session_daemon_launches_detached_module_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change = _make_change(tmp_path, "2026-06-15-abc")
    captured: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kwargs: Any) -> object:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setenv("IMP_VERIFY_COMMAND", "leaked-gate")
    monkeypatch.setenv("IMP_REMOTE_CONTROL_SOCK", "/tmp/stale.sock")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    spawn_session_daemon(change)

    assert captured["argv"] == [
        sys.executable, "-m", "imp.remote.session.daemon",
        "--control-root", str(tmp_path),
        "--change-id", "2026-06-15-abc",
    ]
    kw = captured["kwargs"]
    assert kw["cwd"] == str(tmp_path)
    assert kw["start_new_session"] is True
    assert kw["stdin"] == subprocess.DEVNULL
    assert kw["stdout"] == subprocess.DEVNULL
    assert kw["stderr"] == subprocess.DEVNULL
    assert "IMP_VERIFY_COMMAND" not in kw["env"]
    assert "IMP_REMOTE_CONTROL_SOCK" not in kw["env"]


@pytest.mark.anyio
async def test_attaches_to_resident_daemon_without_spawning(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-res")
    sock = str(session_socket_path(change.control_root, change.id))
    server = SessionSocket(sock)
    spawned: list[Change] = []
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)
        client = await attach_or_spawn(
            change, on_event=lambda _e: None, spawn_fn=spawned.append)
        with anyio.fail_after(5):
            while server.attached_count != 1:
                await anyio.sleep(0.02)
        assert spawned == []        # a resident daemon is attached, never respawned
        await client.aclose()
        await server.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_spawns_then_attaches_when_no_daemon_resident(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-hib")
    sock = str(session_socket_path(change.control_root, change.id))
    received: list[dict[str, Any]] = []
    holder: dict[str, SessionSocket] = {}

    async with anyio.create_task_group() as tg:
        async def _start_server() -> None:
            server = SessionSocket(sock)
            await server.open()
            holder["server"] = server
            await server.serve()

        spawned: list[Change] = []

        def fake_spawn(ch: Change) -> None:
            spawned.append(ch)
            tg.start_soon(_start_server)

        client = await attach_or_spawn(
            change, on_event=received.append, spawn_fn=fake_spawn)
        assert spawned == [change]      # no resident daemon -> spawned exactly once
        with anyio.fail_after(5):
            while holder.get("server") is None or holder["server"].attached_count != 1:
                await anyio.sleep(0.02)
        tg.start_soon(client.receive_events)
        await holder["server"].broadcast(
            {"type": "step_started", "payload": {"step": "plan"}})
        with anyio.fail_after(5):
            while not received:
                await anyio.sleep(0.02)
        assert received == [{"type": "step_started", "payload": {"step": "plan"}}]
        await client.aclose()
        await holder["server"].close()
        tg.cancel_scope.cancel()


def test_spawn_session_daemon_includes_seed_opening_when_provided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change = _make_change(tmp_path, "2026-06-15-abc")
    captured: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kwargs: Any) -> object:
        captured["argv"] = argv
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    spawn_session_daemon(change, seed_opening="ship the\nthing")

    assert captured["argv"] == [
        sys.executable, "-m", "imp.remote.session.daemon",
        "--control-root", str(tmp_path),
        "--change-id", "2026-06-15-abc",
        "--seed-opening", "ship the\nthing",
    ]


@pytest.mark.anyio
async def test_raises_when_spawn_never_creates_the_socket(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-dead")
    with pytest.raises(TimeoutError):
        await attach_or_spawn(
            change, on_event=lambda _e: None,
            spawn_fn=lambda _ch: None, connect_timeout=0.3)
