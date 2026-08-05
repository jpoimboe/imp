"""imp.api.attach: resolution, lease, resume selection, replay handshake, teardown."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.api import (
    DaemonStartupError,
    DaemonUnreachable,
    ImpError,
    QuestHandle,
    QuestNotFound,
    attach,
    create_quest,
)
from imp.engine.state import State
from imp.paths import project_state_dir
from imp.remote.session.discover import session_socket_path
from imp.remote.session.server import SessionServer
from imp.remote.session.socket_server import SessionSocket
from tests.async_wait import wait_until
from tests.conftest import _seed_control

# Real AF_UNIX sockets under `-n auto`: the global 60s cap can fire on scheduling
# starvation alone, so give this file the same ceiling the session-socket family gets.
pytestmark = pytest.mark.timeout(240)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _draft(root: Path) -> QuestHandle:
    _seed_control(root)
    return create_quest(root, "interview me")


def _state_dir(root: Path, quest_id: str) -> Path:
    return project_state_dir(root) / "quests" / quest_id / "state"


@pytest.mark.anyio
async def test_attaching_to_an_unknown_quest_raises_quest_not_found(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)

    with pytest.raises(QuestNotFound) as excinfo:
        async with attach(tmp_path, "2026-01-01-nope"):
            pass

    assert "2026-01-01-nope" in str(excinfo.value)
    assert isinstance(excinfo.value, ImpError)


@pytest.mark.anyio
async def test_a_daemon_that_never_comes_up_raises_daemon_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = _draft(tmp_path)
    # Spawn is a no-op, so nothing ever binds the socket and the connect budget lapses.
    monkeypatch.setattr(
        "imp.remote.session.attach.spawn_session_daemon", lambda quest, **_kw: None)
    monkeypatch.setenv("IMP_SESSION_CONNECT_TIMEOUT", "0.5")

    with pytest.raises(DaemonUnreachable) as excinfo:
        async with attach(tmp_path, handle):      # a QuestHandle is accepted directly
            pass

    # fail_after's TimeoutError has an empty str(); the message must not end in ": ".
    assert str(excinfo.value).endswith("timed out waiting for it to come up")
    assert handle.id in str(excinfo.value)


@pytest.mark.anyio
async def test_attach_streams_live_events_and_releases_the_socket_on_exit(
    tmp_path: Path,
) -> None:
    handle = _draft(tmp_path)
    sock = str(session_socket_path(handle.id))
    server = SessionSocket(sock)

    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)

        async with attach(tmp_path, handle) as session:
            await wait_until(lambda: server.attached_count == 1)
            server.broadcast({"type": "step_started", "payload": {"step": "plan"}})
            got = []
            async for event in session.events():
                got.append(event)
                if event.type == "step_started":
                    break
            # A bare SessionSocket sends no replay snapshot, so this is the whole stream.
            assert [(e.type, e.payload) for e in got] == [
                ("step_started", {"step": "plan"})]

        # AC 3(b): the daemon sees the connection go, so no fd was left open...
        await wait_until(lambda: server.attached_count == 0)
        # ...and the read task ended, closing the feed — this comprehension terminating
        # at all is the assertion; a leaked pump would hang here until the outer cap.
        assert [e async for e in session.events()] == []
        # Both halves are closed, so nothing is left for the GC to reclaim. An
        # open-but-drained receive half would raise EndOfStream here instead.
        with pytest.raises(anyio.ClosedResourceError):
            session._feed.recv.receive_nowait()

        await server.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_the_default_attach_holds_a_lease_and_lease_false_does_not(
    tmp_path: Path,
) -> None:
    handle = _draft(tmp_path)
    sock = str(session_socket_path(handle.id))
    server = SessionSocket(sock)

    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)

        async with attach(tmp_path, handle):
            await wait_until(lambda: server.leased_count == 1)
        await wait_until(lambda: server.attached_count == 0)

        async with attach(tmp_path, handle, lease=False):
            await wait_until(lambda: server.attached_count == 1)
            assert server.leased_count == 0
        # close() awaits aclose() on every still-registered client, and the server
        # drops a detached one only when its read loop wakes on EOF — a later
        # scheduler turn. Closing before that lands raises InvalidStateError out of
        # anyio's stream teardown, so wait for the drop rather than race it.
        await wait_until(lambda: server.attached_count == 0)

        await server.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
@pytest.mark.parametrize("has_started,want_resume", [(False, False), (True, True)])
async def test_the_spawned_daemon_resumes_only_a_quest_that_has_already_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    has_started: bool, want_resume: bool,
) -> None:
    handle = _draft(tmp_path)
    if has_started:
        state = State.load(_state_dir(tmp_path, handle.id))
        state.has_started = True
        state.save(_state_dir(tmp_path, handle.id))
    sock = str(session_socket_path(handle.id))
    resumes: list[bool] = []
    holder: dict[str, SessionSocket] = {}

    async with anyio.create_task_group() as tg:
        async def _start_server() -> None:
            server = SessionSocket(sock)
            await server.open()
            holder["server"] = server
            await server.serve()

        def _fake_spawn(quest: Any, *, resume: bool = False, **_kw: Any) -> None:
            resumes.append(resume)
            tg.start_soon(_start_server)
            return None

        monkeypatch.setattr(
            "imp.remote.session.attach.spawn_session_daemon", _fake_spawn)

        async with attach(tmp_path, handle):
            await wait_until(
                lambda: holder.get("server") is not None
                and holder["server"].attached_count == 1)

        assert resumes == [want_resume]
        # As above: never close the server while a detached client is still registered.
        await wait_until(lambda: holder["server"].attached_count == 0)
        await holder["server"].close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_replay_prefix_then_one_boundary_then_the_live_gate(
    tmp_path: Path,
) -> None:
    handle = _draft(tmp_path)
    quest_dir = project_state_dir(tmp_path) / "quests" / handle.id
    (quest_dir / "events.jsonl").write_text(
        json.dumps({"id": "e1", "type": "step_started",
                    "payload": {"step": "plan"}}) + "\n"
        + json.dumps({"id": "e2", "type": "awaiting_user_input",
                      "payload": {"prompt": "?"}}) + "\n")
    sock = str(session_socket_path(handle.id))
    server = SessionServer(
        sock, events_path=quest_dir / "events.jsonl", hibernate=False)
    answered: dict[str, str] = {}

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "?"}})
        answered["text"] = await user_chat_input_fn("?")
        return "done"

    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve, run_fn)

        async with attach(tmp_path, handle) as session:
            got = []
            async for event in session.events():
                got.append(event)
                if event.type == "awaiting_user_input" and not event.raw.get("_replay"):
                    break
            # The gate legitimately appears twice — once in replay, once live.
            assert [e.type for e in got] == [
                "step_started", "awaiting_user_input", "replay_complete",
                "awaiting_user_input"]
            assert [e.raw.get("_replay") for e in got] == [True, True, None, None]
            assert sum(e.type == "replay_complete" for e in got) == 1
            # Keying on the LIVE copy is what makes this reply legal.
            await session.send_input("us")
            await wait_until(lambda: "text" in answered)

        assert answered["text"] == "us"
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_daemon_startup_error_surfaces_from_public_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.api

    handle = _draft(tmp_path)
    quest_dir = project_state_dir(tmp_path) / "quests" / handle.id
    (quest_dir / "daemon.stderr").write_text("daemon-exploded-at-startup\n")

    class _DeadProc:
        returncode = 7

        def poll(self) -> int | None:
            return 7

    monkeypatch.setattr(
        "imp.remote.session.attach.spawn_session_daemon",
        lambda quest, **_kw: _DeadProc())
    monkeypatch.setenv("IMP_SESSION_CONNECT_TIMEOUT", "30")  # hang backstop only

    created: list[Any] = []
    real_feed_cls = imp.api._Feed

    def _record_feed() -> Any:
        f = real_feed_cls()
        created.append(f)
        return f

    monkeypatch.setattr("imp.api._Feed", _record_feed)

    with pytest.raises(DaemonStartupError) as excinfo:
        async with attach(tmp_path, handle):
            pass

    err = excinfo.value
    assert isinstance(err, DaemonUnreachable)
    assert isinstance(err, ImpError)
    assert "daemon-exploded-at-startup" in err.stderr_tail

    with pytest.raises(anyio.ClosedResourceError):
        created[0].recv.receive_nowait()
    with pytest.raises(anyio.ClosedResourceError):
        created[0].on_event({"type": "step_started"})


@pytest.mark.anyio
async def test_broken_handshake_surfaces_as_daemon_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.remote.session.client import SessionClient

    handle = _draft(tmp_path)
    monkeypatch.setattr(
        "imp.remote.session.attach.spawn_session_daemon", lambda quest, **_kw: None)
    monkeypatch.setenv("IMP_SESSION_CONNECT_TIMEOUT", "0.5")

    async def _raise_broken(self: object) -> None:
        raise anyio.BrokenResourceError()

    monkeypatch.setattr(SessionClient, "connect", _raise_broken)

    with pytest.raises(DaemonUnreachable) as excinfo:
        async with attach(tmp_path, handle):
            pass

    assert isinstance(excinfo.value, ImpError)


@pytest.mark.anyio
async def test_an_exception_from_the_body_reaches_the_caller_unwrapped(
    tmp_path: Path,
) -> None:
    handle = _draft(tmp_path)
    sock = str(session_socket_path(handle.id))
    server = SessionSocket(sock)

    class Boom(Exception):
        pass

    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve)

        # anyio wraps whatever the body raises in an ExceptionGroup; a consumer's
        # `except Boom` around `async with attach(...)` must still fire.
        with pytest.raises(Boom):
            async with attach(tmp_path, handle):
                raise Boom("consumer error")

        await wait_until(lambda: server.attached_count == 0)
        await server.close()
        tg.cancel_scope.cancel()
