from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.engine.quest import Quest
from imp.paths import project_state_dir
from imp.remote.session.attach import (
    DaemonStartupError,
    _read_stderr_tail,
    _resolve_connect_timeout,
    attach_or_spawn,
    build_session_attach,
    spawn_session_daemon,
)
from imp.remote.session.discover import session_socket_path
from imp.remote.session.reply import deliver_session_reply
from imp.remote.session.socket_server import SessionSocket
from tests.async_wait import HANG_BACKSTOP, wait_until


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_quest(tmp_path: Path, quest_id: str) -> Quest:
    return Quest(
        id=quest_id,
        title="T",
        base_branch="main",
        control_root=tmp_path,
        project_root=tmp_path,
        quest_dir=project_state_dir(tmp_path) / "quests" / quest_id,
    )


def test_spawn_session_daemon_launches_detached_module_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-abc")
    captured: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kwargs: Any) -> object:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setenv("IMP_TEST_CMD_FAST", "leaked-gate")
    monkeypatch.setenv("IMP_REMOTE_CONTROL_SOCK", "/tmp/stale.sock")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    spawn_session_daemon(quest)

    assert captured["argv"] == [
        sys.executable, "-m", "imp.remote.session.daemon",
        "--control-root", str(tmp_path),
        "--quest-id", "2026-06-15-abc",
    ]
    kw = captured["kwargs"]
    assert kw["cwd"] == str(tmp_path)
    assert kw["start_new_session"] is True
    assert kw["stdin"] == subprocess.DEVNULL
    assert kw["stdout"] == subprocess.DEVNULL
    assert kw["stderr"] is not subprocess.DEVNULL
    assert Path(kw["stderr"].name) == quest.quest_dir / "daemon.stderr"
    assert "IMP_TEST_CMD_FAST" not in kw["env"]
    assert "IMP_REMOTE_CONTROL_SOCK" not in kw["env"]


@pytest.mark.anyio
async def test_attaches_to_resident_daemon_without_spawning(tmp_path: Path) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-res")
    sock = str(session_socket_path(quest.id))
    server = SessionSocket(sock)
    spawned: list[Quest] = []
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)
        client = await attach_or_spawn(
            quest, on_event=lambda _e: None, spawn_fn=spawned.append)
        await wait_until(lambda: server.attached_count == 1)
        assert spawned == []        # a resident daemon is attached, never respawned
        await client.aclose()
        await server.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_spawns_then_attaches_when_no_daemon_resident(tmp_path: Path) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-hib")
    sock = str(session_socket_path(quest.id))
    received: list[dict[str, Any]] = []
    holder: dict[str, SessionSocket] = {}

    async with anyio.create_task_group() as tg:
        async def _start_server() -> None:
            server = SessionSocket(sock)
            await server.open()
            holder["server"] = server
            await server.serve()

        spawned: list[Quest] = []

        def fake_spawn(ch: Quest) -> None:
            spawned.append(ch)
            tg.start_soon(_start_server)

        client = await attach_or_spawn(
            quest, on_event=received.append, spawn_fn=fake_spawn)
        assert spawned == [quest]      # no resident daemon -> spawned exactly once
        await wait_until(
            lambda: holder.get("server") is not None and holder["server"].attached_count == 1)
        tg.start_soon(client.receive_events)
        holder["server"].broadcast(
            {"type": "step_started", "payload": {"step": "plan"}})
        await wait_until(lambda: received)
        assert received == [{"type": "step_started", "payload": {"step": "plan"}}]
        await client.aclose()
        await holder["server"].close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_raises_when_spawn_never_creates_the_socket(tmp_path: Path) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-dead")
    with pytest.raises(TimeoutError):
        await attach_or_spawn(
            quest, on_event=lambda _e: None,
            spawn_fn=lambda _ch: None, connect_timeout=0.3)


@pytest.mark.anyio
async def test_attach_crash_fast_when_daemon_exits_before_binding(
    tmp_path: Path,
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-crash")
    quest.quest_dir.mkdir(parents=True, exist_ok=True)
    (quest.quest_dir / "daemon.stderr").write_bytes(
        b"Traceback (most recent call last):\nRuntimeError: boom-on-startup\n")

    class _DeadProc:
        returncode = 7

        def poll(self) -> int | None:
            return 7

    with pytest.raises(DaemonStartupError) as excinfo:
        await attach_or_spawn(
            quest, on_event=lambda _e: None,
            spawn_fn=lambda _ch: _DeadProc(), connect_timeout=HANG_BACKSTOP)

    err = excinfo.value
    assert err.exit_code == 7
    assert err.stderr_path == quest.quest_dir / "daemon.stderr"
    assert "boom-on-startup" in err.stderr_tail
    assert quest.id in str(err)
    assert "7" in str(err)


def test_read_stderr_tail_returns_empty_when_file_is_missing(tmp_path: Path) -> None:
    assert _read_stderr_tail(tmp_path / "does-not-exist.stderr") == ""


@pytest.mark.anyio
async def test_attach_crash_fast_when_stderr_file_is_missing(
    tmp_path: Path,
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-nofile")

    class _DeadProc:
        returncode = 3

        def poll(self) -> int | None:
            return 3

    with pytest.raises(DaemonStartupError) as excinfo:
        await attach_or_spawn(
            quest, on_event=lambda _e: None,
            spawn_fn=lambda _ch: _DeadProc(), connect_timeout=HANG_BACKSTOP)

    err = excinfo.value
    assert err.exit_code == 3
    assert err.stderr_tail == ""
    assert quest.id in str(err)


@pytest.mark.anyio
async def test_attach_waits_for_alive_daemon_until_socket_appears(
    tmp_path: Path,
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-slow")
    sock = str(session_socket_path(quest.id))
    holder: dict[str, SessionSocket] = {}

    class _LiveProc:
        returncode = None

        def poll(self) -> None:
            return None

    async with anyio.create_task_group() as tg:
        async def _start_server_after_delay() -> None:
            await anyio.sleep(0.3)
            server = SessionSocket(sock)
            await server.open()
            holder["server"] = server
            await server.serve()

        def slow_spawn(ch: Quest) -> _LiveProc:
            tg.start_soon(_start_server_after_delay)
            return _LiveProc()

        client = await attach_or_spawn(
            quest, on_event=lambda _e: None,
            spawn_fn=slow_spawn, connect_timeout=HANG_BACKSTOP)
        await wait_until(
            lambda: holder.get("server") is not None and holder["server"].attached_count == 1)
        assert holder["server"].attached_count == 1
        await client.aclose()
        await holder["server"].close()
        tg.cancel_scope.cancel()


def test_spawn_session_daemon_includes_resume_flag_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-abc")
    captured: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kwargs: Any) -> object:
        captured["argv"] = argv
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    spawn_session_daemon(quest, resume=True)

    assert captured["argv"] == [
        sys.executable, "-m", "imp.remote.session.daemon",
        "--control-root", str(tmp_path),
        "--quest-id", "2026-06-15-abc",
        "--resume",
    ]


@pytest.mark.anyio
async def test_build_session_attach_uses_resident_daemon_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from imp.remote.session import attach as attach_mod

    quest = _make_quest(tmp_path, "2026-06-15-warm")
    sock = str(session_socket_path(quest.id))
    spawned: list[Quest] = []
    monkeypatch.setattr(
        attach_mod, "spawn_session_daemon",
        lambda ch, *, resume=False,
        non_interactive=False, autonomous=False, model=None,
        opening_unsandboxed=False: spawned.append(ch))

    received: list[dict[str, Any]] = []
    server = SessionSocket(sock)
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)

        attach = attach_mod.build_session_attach(quest)
        client = await attach(received.append)
        await wait_until(lambda: server.attached_count == 1)
        assert spawned == []        # resident daemon attached, never respawned

        tg.start_soon(client.receive_events)
        server.broadcast({"type": "step_started", "payload": {"step": "plan"}})
        await wait_until(lambda: received)
        assert received == [{"type": "step_started", "payload": {"step": "plan"}}]

        await client.aclose()
        await server.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_build_session_attach_threads_resume_into_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from imp.remote.session import attach as attach_mod

    quest = _make_quest(tmp_path, "2026-06-15-rsm")
    sock = str(session_socket_path(quest.id))
    captured: dict[str, Any] = {}
    holder: dict[str, SessionSocket] = {}

    async with anyio.create_task_group() as tg:
        async def _start_server() -> None:
            server = SessionSocket(sock)
            await server.open()
            holder["server"] = server
            await server.serve()

        def fake_spawn(
            ch: Quest, *, resume: bool = False,
            non_interactive: bool = False, autonomous: bool = False,
            model: str | None = None, opening_unsandboxed: bool = False,
        ) -> None:
            captured["resume"] = resume
            tg.start_soon(_start_server)

        monkeypatch.setattr(attach_mod, "spawn_session_daemon", fake_spawn)

        attach = attach_mod.build_session_attach(quest, resume=True)
        client = await attach(lambda _e: None)

        assert captured["resume"] is True
        await wait_until(
            lambda: holder.get("server") is not None and holder["server"].attached_count == 1)
        await client.aclose()
        await holder["server"].close()
        tg.cancel_scope.cancel()


def test_spawn_session_daemon_includes_autonomous_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-abc")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: captured.update(argv=argv) or object())
    spawn_session_daemon(quest, autonomous=True)
    assert "--autonomous" in captured["argv"]


def test_spawn_session_daemon_omits_autonomous_when_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-abc")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: captured.update(argv=argv) or object())
    spawn_session_daemon(quest)
    assert "--autonomous" not in captured["argv"]


@pytest.mark.anyio
async def test_build_session_attach_leases_when_requested(tmp_path: Path) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-lz")
    sock = str(session_socket_path(quest.id))
    seen: list[Any] = []
    server = SessionSocket(sock, on_attach=lambda hello, _send: seen.append(hello))
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)
        attach = build_session_attach(quest, lease=True)
        client = await attach(lambda _e: None)
        await wait_until(lambda: seen)
        assert seen[0].lease is True   # the terminal opts in to a keep-alive lease
        await client.aclose()
        await server.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_deliver_session_reply_attaches_lease_less(tmp_path: Path) -> None:
    # A remote reply goes through the DEFAULT attach (build_session_attach with no
    # lease=). It must NOT hold a keep-alive lease, or a reply that rehydrates a
    # hibernated change would pause the run the instant it detaches.
    quest = _make_quest(tmp_path, "2026-06-15-rep")
    sock = str(session_socket_path(quest.id))
    seen: list[Any] = []
    server = SessionSocket(sock, on_attach=lambda hello, _send: seen.append(hello))
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)

        async def _gate_once_attached() -> None:
            await wait_until(lambda: server.attached_count != 0)
            server.broadcast(
                {"type": "awaiting_user_input", "payload": {"prompt": "?"}})

        tg.start_soon(_gate_once_attached)
        await deliver_session_reply(quest, "my reply")
        assert seen and seen[0].lease is False
        await server.close()
        tg.cancel_scope.cancel()


def test_spawn_session_daemon_captures_child_stderr_to_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-err")

    def fake_popen(argv: list[str], *, stderr: Any, **kw: Any) -> object:
        stderr.write(b"Traceback (most recent call last):\nBoom\n")
        stderr.flush()
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    spawn_session_daemon(quest)

    captured = (quest.quest_dir / "daemon.stderr").read_bytes()
    assert b"Traceback (most recent call last):" in captured
    assert b"Boom" in captured


def test_spawn_session_daemon_truncates_stderr_file_on_respawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-trunc")

    def first(argv: list[str], *, stderr: Any, **kw: Any) -> object:
        stderr.write(b"first-segment-crash\n")
        return object()

    monkeypatch.setattr(subprocess, "Popen", first)
    spawn_session_daemon(quest)

    def second(argv: list[str], *, stderr: Any, **kw: Any) -> object:
        stderr.write(b"second\n")
        return object()

    monkeypatch.setattr(subprocess, "Popen", second)
    spawn_session_daemon(quest)

    captured = (quest.quest_dir / "daemon.stderr").read_bytes()
    assert captured == b"second\n"          # truncated ("wb"), not appended
    assert b"first-segment-crash" not in captured


def test_spawn_session_daemon_appends_model_arg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-model")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: captured.update(argv=argv) or object())
    spawn_session_daemon(quest, model="anthropic:claude-fable-5")
    assert "--model" in captured["argv"]
    i = captured["argv"].index("--model")
    assert captured["argv"][i + 1] == "anthropic:claude-fable-5"


def test_spawn_session_daemon_appends_unsandboxed_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-unsbx")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: captured.update(argv=argv) or object())
    spawn_session_daemon(quest, opening_unsandboxed=True)
    assert "--unsandboxed" in captured["argv"]


def test_spawn_session_daemon_omits_unsandboxed_when_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-sbx")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: captured.update(argv=argv) or object())
    spawn_session_daemon(quest)
    assert "--unsandboxed" not in captured["argv"]


@pytest.mark.anyio
async def test_build_session_attach_threads_unsandboxed_into_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from imp.remote.session import attach as attach_mod

    quest = _make_quest(tmp_path, "2026-06-15-unsbx2")
    sock = str(session_socket_path(quest.id))
    captured: dict[str, Any] = {}
    holder: dict[str, SessionSocket] = {}

    async with anyio.create_task_group() as tg:
        async def _start_server() -> None:
            server = SessionSocket(sock)
            await server.open()
            holder["server"] = server
            await server.serve()

        def fake_spawn(
            ch: Quest, *, resume: bool = False,
            non_interactive: bool = False, autonomous: bool = False,
            model: str | None = None, opening_unsandboxed: bool = False,
        ) -> None:
            captured["opening_unsandboxed"] = opening_unsandboxed
            tg.start_soon(_start_server)

        monkeypatch.setattr(attach_mod, "spawn_session_daemon", fake_spawn)

        attach = attach_mod.build_session_attach(quest, opening_unsandboxed=True)
        client = await attach(lambda _e: None)

        assert captured["opening_unsandboxed"] is True
        await wait_until(
            lambda: holder.get("server") is not None and holder["server"].attached_count == 1)
        await client.aclose()
        await holder["server"].close()
        tg.cancel_scope.cancel()


def test_spawn_session_daemon_omits_model_when_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-abc")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: captured.update(argv=argv) or object())
    spawn_session_daemon(quest)
    assert "--model" not in captured["argv"]


def test_spawn_session_daemon_returns_the_popen_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-handle")
    sentinel = object()
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: sentinel)

    assert spawn_session_daemon(quest) is sentinel


def test_resolve_connect_timeout_reads_env_when_arg_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMP_SESSION_CONNECT_TIMEOUT", "12.5")
    assert _resolve_connect_timeout(None) == 12.5


def test_resolve_connect_timeout_defaults_to_60_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IMP_SESSION_CONNECT_TIMEOUT", raising=False)
    assert _resolve_connect_timeout(None) == 60.0


@pytest.mark.anyio
async def test_broken_handshake_surfaces_as_daemon_startup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.remote.session.client import SessionClient

    quest = _make_quest(tmp_path, "2026-06-15-brkhs")
    quest.quest_dir.mkdir(parents=True, exist_ok=True)
    (quest.quest_dir / "daemon.stderr").write_bytes(b"handshake-died\n")

    class _DeadProc:
        returncode = 7

        def poll(self) -> int | None:
            return 7

    async def _raise_broken(self: Any) -> None:
        raise anyio.BrokenResourceError()

    monkeypatch.setattr(SessionClient, "connect", _raise_broken)

    with pytest.raises(DaemonStartupError):
        await attach_or_spawn(
            quest, on_event=lambda _e: None,
            spawn_fn=lambda _ch: _DeadProc(), connect_timeout=HANG_BACKSTOP)
