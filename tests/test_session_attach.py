from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.engine.change import Change
from imp.remote.session.attach import attach_or_spawn, build_session_attach, spawn_session_daemon
from imp.remote.session.discover import session_socket_path
from imp.remote.session.reply import deliver_session_reply
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

    monkeypatch.setenv("IMP_TEST_CMD_FAST", "leaked-gate")
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
    assert kw["stderr"] is not subprocess.DEVNULL
    assert Path(kw["stderr"].name) == change.change_dir / "daemon.stderr"
    assert "IMP_TEST_CMD_FAST" not in kw["env"]
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
        holder["server"].broadcast(
            {"type": "step_started", "payload": {"step": "plan"}})
        with anyio.fail_after(5):
            while not received:
                await anyio.sleep(0.02)
        assert received == [{"type": "step_started", "payload": {"step": "plan"}}]
        await client.aclose()
        await holder["server"].close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_raises_when_spawn_never_creates_the_socket(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-dead")
    with pytest.raises(TimeoutError):
        await attach_or_spawn(
            change, on_event=lambda _e: None,
            spawn_fn=lambda _ch: None, connect_timeout=0.3)


def test_spawn_session_daemon_includes_resume_flag_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change = _make_change(tmp_path, "2026-06-15-abc")
    captured: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kwargs: Any) -> object:
        captured["argv"] = argv
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    spawn_session_daemon(change, resume=True)

    assert captured["argv"] == [
        sys.executable, "-m", "imp.remote.session.daemon",
        "--control-root", str(tmp_path),
        "--change-id", "2026-06-15-abc",
        "--resume",
    ]


@pytest.mark.anyio
async def test_build_session_attach_uses_resident_daemon_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from imp.remote.session import attach as attach_mod

    change = _make_change(tmp_path, "2026-06-15-warm")
    sock = str(session_socket_path(change.control_root, change.id))
    spawned: list[Change] = []
    monkeypatch.setattr(
        attach_mod, "spawn_session_daemon",
        lambda ch, *, resume=False,
        non_interactive=False, autonomous=False, model=None: spawned.append(ch))

    received: list[dict[str, Any]] = []
    server = SessionSocket(sock)
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)

        attach = attach_mod.build_session_attach(change)
        client = await attach(received.append)
        with anyio.fail_after(5):
            while server.attached_count != 1:
                await anyio.sleep(0.02)
        assert spawned == []        # resident daemon attached, never respawned

        tg.start_soon(client.receive_events)
        server.broadcast({"type": "step_started", "payload": {"step": "plan"}})
        with anyio.fail_after(5):
            while not received:
                await anyio.sleep(0.02)
        assert received == [{"type": "step_started", "payload": {"step": "plan"}}]

        await client.aclose()
        await server.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_build_session_attach_threads_resume_into_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from imp.remote.session import attach as attach_mod

    change = _make_change(tmp_path, "2026-06-15-rsm")
    sock = str(session_socket_path(change.control_root, change.id))
    captured: dict[str, Any] = {}
    holder: dict[str, SessionSocket] = {}

    async with anyio.create_task_group() as tg:
        async def _start_server() -> None:
            server = SessionSocket(sock)
            await server.open()
            holder["server"] = server
            await server.serve()

        def fake_spawn(
            ch: Change, *, resume: bool = False,
            non_interactive: bool = False, autonomous: bool = False,
            model: str | None = None,
        ) -> None:
            captured["resume"] = resume
            tg.start_soon(_start_server)

        monkeypatch.setattr(attach_mod, "spawn_session_daemon", fake_spawn)

        attach = attach_mod.build_session_attach(change, resume=True)
        client = await attach(lambda _e: None)

        assert captured["resume"] is True
        with anyio.fail_after(5):
            while holder.get("server") is None or holder["server"].attached_count != 1:
                await anyio.sleep(0.02)
        await client.aclose()
        await holder["server"].close()
        tg.cancel_scope.cancel()


def test_spawn_session_daemon_includes_autonomous_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change = _make_change(tmp_path, "2026-06-15-abc")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: captured.update(argv=argv) or object())
    spawn_session_daemon(change, autonomous=True)
    assert "--autonomous" in captured["argv"]


def test_spawn_session_daemon_omits_autonomous_when_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change = _make_change(tmp_path, "2026-06-15-abc")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: captured.update(argv=argv) or object())
    spawn_session_daemon(change)
    assert "--autonomous" not in captured["argv"]


@pytest.mark.anyio
async def test_build_session_attach_leases_when_requested(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-lz")
    sock = str(session_socket_path(change.control_root, change.id))
    seen: list[Any] = []
    server = SessionSocket(sock, on_attach=lambda hello, _send: seen.append(hello))
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)
        attach = build_session_attach(change, lease=True)
        client = await attach(lambda _e: None)
        with anyio.fail_after(5):
            while not seen:
                await anyio.sleep(0.02)
        assert seen[0].lease is True   # the terminal opts in to a keep-alive lease
        await client.aclose()
        await server.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_deliver_session_reply_attaches_lease_less(tmp_path: Path) -> None:
    # A remote reply goes through the DEFAULT attach (build_session_attach with no
    # lease=). It must NOT hold a keep-alive lease, or a reply that rehydrates a
    # hibernated change would pause the run the instant it detaches.
    change = _make_change(tmp_path, "2026-06-15-rep")
    sock = str(session_socket_path(change.control_root, change.id))
    seen: list[Any] = []
    server = SessionSocket(sock, on_attach=lambda hello, _send: seen.append(hello))
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)

        async def _gate_once_attached() -> None:
            with anyio.fail_after(5):
                while server.attached_count == 0:
                    await anyio.sleep(0.02)
            server.broadcast(
                {"type": "awaiting_user_input", "payload": {"prompt": "?"}})

        tg.start_soon(_gate_once_attached)
        await deliver_session_reply(change, "my reply")
        assert seen and seen[0].lease is False
        await server.close()
        tg.cancel_scope.cancel()


def test_spawn_session_daemon_captures_child_stderr_to_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change = _make_change(tmp_path, "2026-06-15-err")

    def fake_popen(argv: list[str], *, stderr: Any, **kw: Any) -> object:
        stderr.write(b"Traceback (most recent call last):\nBoom\n")
        stderr.flush()
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    spawn_session_daemon(change)

    captured = (change.change_dir / "daemon.stderr").read_bytes()
    assert b"Traceback (most recent call last):" in captured
    assert b"Boom" in captured


def test_spawn_session_daemon_truncates_stderr_file_on_respawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change = _make_change(tmp_path, "2026-06-15-trunc")

    def first(argv: list[str], *, stderr: Any, **kw: Any) -> object:
        stderr.write(b"first-segment-crash\n")
        return object()

    monkeypatch.setattr(subprocess, "Popen", first)
    spawn_session_daemon(change)

    def second(argv: list[str], *, stderr: Any, **kw: Any) -> object:
        stderr.write(b"second\n")
        return object()

    monkeypatch.setattr(subprocess, "Popen", second)
    spawn_session_daemon(change)

    captured = (change.change_dir / "daemon.stderr").read_bytes()
    assert captured == b"second\n"          # truncated ("wb"), not appended
    assert b"first-segment-crash" not in captured


def test_spawn_session_daemon_appends_model_arg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change = _make_change(tmp_path, "2026-06-15-model")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: captured.update(argv=argv) or object())
    spawn_session_daemon(change, model="anthropic:claude-fable-5")
    assert "--model" in captured["argv"]
    i = captured["argv"].index("--model")
    assert captured["argv"][i + 1] == "anthropic:claude-fable-5"


def test_spawn_session_daemon_omits_model_when_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    change = _make_change(tmp_path, "2026-06-15-abc")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: captured.update(argv=argv) or object())
    spawn_session_daemon(change)
    assert "--model" not in captured["argv"]
