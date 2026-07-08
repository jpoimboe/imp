from __future__ import annotations

import functools
import json
import logging
import signal
import threading
import time
from pathlib import Path

import anyio
import pytest

from imp.remote.daemon.alert_all import AlertAll
from imp.remote.daemon.commands import Help
from imp.remote.daemon.discover import socket_is_live, socket_path
from imp.remote.daemon.runner import (
    _await_shutdown_signals,
    _poll_worker,
    _sender_worker,
    pid_path,
    run_daemon,
)
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
    home = tmp_path / ".imp" / "changes" / cid
    home.mkdir(parents=True, exist_ok=True)
    (home / "change.json").write_text("{}")
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
            make_adapter=lambda env, submit, subscriptions, alert_all: adapter,
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
            make_adapter=lambda env, submit, subscriptions, alert_all: SubmitOnceAdapter(submit),
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
                make_adapter=lambda env, submit, subscriptions, alert_all: adapter,
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


@pytest.mark.anyio
async def test_workers_run_as_daemon_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    created: list[threading.Thread] = []
    real_thread = threading.Thread

    def recording_thread(*args, **kwargs):
        # Subclass to preserve _target after Thread.run() deletes it on completion
        class _T(real_thread):
            def run(self):
                saved = self._target
                super().run()
                self._target = saved
        t = _T(*args, **kwargs)
        created.append(t)
        return t

    monkeypatch.setattr(
        "imp.remote.daemon.runner.threading.Thread", recording_thread)
    shutdown = anyio.Event()
    spath = socket_path(tmp_path)

    async with anyio.create_task_group() as tg:
        tg.start_soon(functools.partial(
            run_daemon, tmp_path, env={},
            make_adapter=lambda env, submit, subscriptions, alert_all: FakeAdapter(),
            install_signals=False, shutdown=shutdown))
        with anyio.fail_after(5):
            while not socket_is_live(spath):
                await anyio.sleep(0.02)
        shutdown.set()

    workers = [
        t for t in created
        if getattr(t, "_target", None) in (_poll_worker, _sender_worker)]
    assert len(workers) == 2
    assert all(t.daemon for t in workers)


@pytest.mark.anyio
async def test_run_daemon_returns_while_poll_worker_is_blocked(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    block = threading.Event()

    class BlockingAdapter:
        def on_event(self, event: dict) -> None: ...
        def on_response(self, response: object) -> None: ...
        def poll_once(self) -> None:
            block.wait(30)   # a long-poll syscall that `stop` cannot interrupt

    shutdown = anyio.Event()
    spath = socket_path(tmp_path)
    start = time.monotonic()
    try:
        with anyio.fail_after(5):
            async with anyio.create_task_group() as tg:
                tg.start_soon(functools.partial(
                    run_daemon, tmp_path, env={},
                    make_adapter=lambda env, submit, subscriptions, alert_all: BlockingAdapter(),
                    install_signals=False, shutdown=shutdown))
                with anyio.fail_after(5):
                    while not socket_is_live(spath):
                        await anyio.sleep(0.02)
                shutdown.set()
    finally:
        block.set()   # release the parked daemon thread so it exits cleanly
    assert time.monotonic() - start < 3


class _FakeSignals:
    """A sync context manager (mirroring anyio.open_signal_receiver) whose
    async-iterable body yields a controlled, finite stream of signal numbers."""

    def __init__(self, signums: list[int]) -> None:
        self._signums = list(signums)

    def __enter__(self) -> _FakeSignals:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def __aiter__(self) -> _FakeSignals:
        return self

    async def __anext__(self) -> int:
        if not self._signums:
            raise StopAsyncIteration
        return self._signums.pop(0)


@pytest.mark.anyio
async def test_await_shutdown_signals_drains_then_escalates(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        "imp.remote.daemon.runner.anyio.open_signal_receiver",
        lambda *sigs: _FakeSignals([signal.SIGINT, signal.SIGINT]))
    shutdown = anyio.Event()
    exits: list[int] = []
    caplog.set_level(logging.INFO, logger="imp.daemon")

    await _await_shutdown_signals(shutdown, force_exit=exits.append)

    assert shutdown.is_set()                       # first signal drained gracefully
    assert exits == [0]                            # second signal force-exited once
    assert any(
        "Ctrl-C again to force-quit" in record.message
        for record in caplog.records)


@pytest.mark.anyio
async def test_run_daemon_injects_a_default_on_alert_all_into_the_adapter(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    captured: dict = {}

    def make_adapter(env, submit, subscriptions, alert_all):
        captured["alert_all"] = alert_all
        return FakeAdapter()

    shutdown = anyio.Event()
    async with anyio.create_task_group() as tg:
        tg.start_soon(functools.partial(
            run_daemon, tmp_path, env={},
            make_adapter=make_adapter, install_signals=False, shutdown=shutdown))
        with anyio.fail_after(5):
            while "alert_all" not in captured:
                await anyio.sleep(0.02)
        shutdown.set()

    assert isinstance(captured["alert_all"], AlertAll)
    assert captured["alert_all"].enabled() is True


@pytest.mark.anyio
async def test_run_daemon_reaps_archived_homes_at_startup(tmp_path: Path) -> None:
    from imp.engine import archive

    _seed_control(tmp_path)
    cid = "archived-leftover"
    home = tmp_path / ".imp" / "changes" / cid
    (home / "state").mkdir(parents=True)
    (home / "change.json").write_text(json.dumps({
        "id": cid, "title": cid, "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00"}))
    (home / "state" / "state.json").write_text(json.dumps({
        "id": cid, "status": "completed", "step": None}))
    (home / "events.jsonl").write_text(
        '{"id":"e0","parent_id":null,"agent_id":null,'
        '"timestamp":"2026-06-01T00:00:01+00:00","type":"change_created",'
        '"payload":{"id":"archived-leftover"}}\n')
    archive.archive_change(
        tmp_path, cid, events_path=home / "events.jsonl",
        change_json_path=home / "change.json",
        state_json_path=home / "state" / "state.json")

    shutdown = anyio.Event()
    log_path = tmp_path / ".imp" / "remote-control.log"
    spath = socket_path(tmp_path)

    async with anyio.create_task_group() as tg:
        tg.start_soon(functools.partial(
            run_daemon, tmp_path, env={},
            make_adapter=lambda env, submit, subscriptions, alert_all: FakeAdapter(),
            install_signals=False, shutdown=shutdown))
        # The socket only binds AFTER the startup reap runs, so socket-live
        # guarantees the sweep already executed.
        with anyio.fail_after(5):
            while not socket_is_live(spath):
                await anyio.sleep(0.02)
        shutdown.set()

    assert not home.exists()                       # leaked home reaped at startup
    assert "reaped 1" in log_path.read_text()       # INFO count logged
