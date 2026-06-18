from __future__ import annotations

import functools
import time
from pathlib import Path

import anyio
import pytest

from imp.remote.daemon.commands import Help
from imp.remote.daemon.discover import ChangeInfo, socket_is_live, socket_path
from imp.remote.daemon.runner import pid_path, run_daemon
from imp.remote.session.discover import session_socket_path
from imp.remote.session.socket_server import SessionSocket
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeAdapter:
    """A north adapter that records relayed events and never polls real HTTP."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def on_event(self, event: dict) -> None:
        self.events.append(event)

    def on_response(self, response: object) -> None:
        pass

    def poll_once(self) -> None:
        time.sleep(0.01)   # pace the worker loop; no inbound to submit


@pytest.mark.anyio
async def test_run_daemon_relays_a_resident_session_event_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    cid = "2026-06-13-abc"
    monkeypatch.setattr(
        "imp.remote.daemon.directory.list_changes",
        lambda _cr: [ChangeInfo(id=cid, title="T", status="running", step="plan")])
    monkeypatch.setattr(
        "imp.remote.daemon.discover.snapshot",
        lambda _cr, c: ("running", "plan", "T") if c == cid else None)
    adapter = FakeAdapter()
    shutdown = anyio.Event()
    spath = socket_path(tmp_path)
    sess = SessionSocket(str(session_socket_path(tmp_path, cid)))
    await sess.open()

    async with anyio.create_task_group() as tg:
        tg.start_soon(sess.serve)
        tg.start_soon(functools.partial(
            run_daemon, tmp_path, env={},
            make_adapter=lambda env, submit, focus: adapter,
            install_signals=False, shutdown=shutdown))

        with anyio.fail_after(5):              # daemon up + pidfile written
            while not (socket_is_live(spath) and pid_path(tmp_path).exists()):
                await anyio.sleep(0.02)

        # The supervisor polls the directory and attaches a relay to the session.
        with anyio.fail_after(5):
            while sess.attached_count == 0:
                await anyio.sleep(0.02)
        sess.broadcast(
            {"id": "e1", "type": "agent_output", "payload": {"text": "hi"}})
        with anyio.fail_after(5):
            while not adapter.events:
                await anyio.sleep(0.02)
        assert adapter.events[0]["type"] == "agent_output"
        assert adapter.events[0]["change"]["id"] == cid

        await sess.close()
        shutdown.set()                          # clean drain

    # After run_daemon returns the per-repo socket is unbound and the pidfile gone.
    assert not socket_is_live(spath)
    assert not pid_path(tmp_path).exists()


class SubmitOnceAdapter:
    def __init__(self, submit) -> None:
        self._submit = submit
        self._done = False

    def on_event(self, event: dict) -> None:
        pass

    def on_response(self, response: object) -> None:
        pass

    def poll_once(self) -> None:
        if not self._done:
            self._done = True
            self._submit(Help())
        time.sleep(0.01)


@pytest.mark.anyio
async def test_run_daemon_logs_start_and_routed_command_to_file(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    shutdown = anyio.Event()
    log_path = tmp_path / ".imp" / "remote-control.log"

    async with anyio.create_task_group() as tg:
        tg.start_soon(functools.partial(
            run_daemon, tmp_path, env={},
            make_adapter=lambda env, submit, focus: SubmitOnceAdapter(submit),
            install_signals=False, shutdown=shutdown))

        with anyio.fail_after(5):
            while not (log_path.exists() and "routing command" in log_path.read_text()):
                await anyio.sleep(0.02)
        shutdown.set()

    text = log_path.read_text()
    assert "remote-control daemon starting" in text
    assert "routing command" in text


@pytest.mark.anyio
async def test_run_daemon_returns_promptly_under_cancellation(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    adapter = FakeAdapter()
    shutdown = anyio.Event()        # never set: only cancellation can stop the daemon
    spath = socket_path(tmp_path)

    start = time.monotonic()
    cancel_time: float = 0.0
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            tg.start_soon(functools.partial(
                run_daemon, tmp_path, env={},
                make_adapter=lambda env, submit, focus: adapter,
                install_signals=False, shutdown=shutdown))

            with anyio.fail_after(5):              # socket comes up
                while not socket_is_live(spath):
                    await anyio.sleep(0.02)
            assert pid_path(tmp_path).exists()

            cancel_time = time.monotonic()
            tg.cancel_scope.cancel()               # external cancel — the Defect-1 path
    elapsed = time.monotonic() - start

    assert elapsed < 3
    assert time.monotonic() - cancel_time < 1.5
    assert not socket_is_live(spath)
    assert not pid_path(tmp_path).exists()
