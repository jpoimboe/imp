from __future__ import annotations

import logging
import os
import queue
import socket
import threading
import urllib.error
from pathlib import Path

import pytest

from imp.remote.daemon import runner
from imp.remote.daemon.discover import socket_path


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_drain_outbound_feeds_each_queued_event_to_on_event() -> None:
    seen: list[dict] = []

    class FakeAdapter:
        def on_event(self, event: dict) -> None:
            seen.append(event)

    q: queue.Queue[dict] = queue.Queue()
    q.put({"type": "a"})
    q.put({"type": "b"})
    runner.drain_outbound(FakeAdapter(), q)
    assert seen == [{"type": "a"}, {"type": "b"}]
    assert q.empty()


def test_pidfile_roundtrip(tmp_path: Path) -> None:
    (tmp_path / ".imp").mkdir()
    runner.write_pid(tmp_path)
    assert runner.read_pid(tmp_path) == os.getpid()
    runner.remove_pid(tmp_path)
    assert runner.read_pid(tmp_path) is None


def test_read_pid_returns_none_for_corrupt_pidfile(tmp_path: Path) -> None:
    (tmp_path / ".imp").mkdir()
    runner.pid_path(tmp_path).write_text("not-a-number")
    assert runner.read_pid(tmp_path) is None


def test_drain_outbound_is_a_no_op_on_empty_queue() -> None:
    seen: list = []

    class FakeAdapter:
        def on_event(self, event: dict) -> None:
            seen.append(event)

    runner.drain_outbound(FakeAdapter(), queue.Queue())
    assert seen == []


def test_check_not_running_raises_when_socket_is_live(tmp_path: Path) -> None:
    (tmp_path / ".imp").mkdir()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(socket_path(tmp_path)))
    srv.listen()
    try:
        with pytest.raises(runner.DaemonAlreadyRunning):
            runner.check_not_running(tmp_path)
    finally:
        srv.close()


def test_check_not_running_passes_when_no_daemon(tmp_path: Path) -> None:
    (tmp_path / ".imp").mkdir()
    runner.check_not_running(tmp_path)   # no raise


def test_worker_gate_logs_once_then_suppresses_until_a_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = runner._WorkerGate()
    with caplog.at_level(logging.DEBUG, logger="imp.daemon"):
        gate.fail(RuntimeError("x"))   # first failure → WARNING
        gate.fail(RuntimeError("y"))   # still failing → DEBUG
        gate.ok()                      # a success re-arms the warning
        gate.fail(RuntimeError("z"))   # next failure → WARNING again
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(warnings) == 2
    assert len(debugs) == 1


def test_worker_gate_omits_traceback_for_transient_network_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A connection reset on the Telegram long-poll is routine and self-healing:
    # one concise WARNING line, no alarming traceback.
    transient = urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))
    with caplog.at_level(logging.WARNING, logger="imp.daemon"):
        runner._WorkerGate().fail(transient)
    (record,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "transient network error" in record.message
    assert record.exc_info is None


def test_worker_gate_keeps_traceback_for_unexpected_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An HTTPError (bad token / conflict) is persistent config, not a blip — it
    # keeps the full traceback so the operator can see what is actually wrong.
    http_error = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger="imp.daemon"):
        runner._WorkerGate().fail(http_error)
    (record,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "transient network error" not in record.message
    assert record.exc_info is not None


class _RaiseThenStopAdapter:
    """poll_once raises on the first call, then sets `stop` on the second."""

    def __init__(self, stop: threading.Event) -> None:
        self._stop = stop
        self.polls = 0

    def on_event(self, event: dict) -> None:
        pass

    def on_response(self, response: object) -> None:
        pass

    def poll_once(self) -> None:
        self.polls += 1
        if self.polls == 1:
            raise RuntimeError("boom")
        self._stop.set()


def test_poll_worker_survives_a_raising_poll_and_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(runner, "_WORKER_GAP", 0.01)   # keep the backoff fast
    stop = threading.Event()
    adapter = _RaiseThenStopAdapter(stop)
    with caplog.at_level(logging.WARNING, logger="imp.daemon"):
        runner._poll_worker(adapter, stop)
    assert adapter.polls == 2                       # loop continued past the raise
    assert "adapter worker cycle failed" in caplog.text


def test_sender_worker_delivers_a_response_without_polling() -> None:
    # The sender is decoupled from the inbound long-poll: it flushes a queued
    # response immediately and never calls poll_once (that is the poll worker's
    # job). This is the fix for replies being gated behind the 25s long-poll.
    from imp.remote.daemon.commands import Notice

    stop = threading.Event()
    responses: queue.Queue = queue.Queue()
    responses.put(Notice("hi"))
    sent: list[str] = []

    class _RecordingAdapter:
        def on_event(self, event: dict) -> None:
            pass

        def on_response(self, response: object) -> None:
            sent.append(response.text)   # type: ignore[attr-defined]
            stop.set()                   # one delivery, then end the loop

        def poll_once(self) -> None:
            raise AssertionError("sender worker must not poll")

    runner._sender_worker(_RecordingAdapter(), queue.Queue(), responses, stop)
    assert sent == ["hi"]


def test_sender_worker_shutdown_survives_a_failing_final_drain(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from imp.remote.daemon.commands import Notice

    stop = threading.Event()
    stop.set()                                       # skip the loop, go to final drain
    responses: queue.Queue = queue.Queue()
    responses.put(Notice("bye"))

    class _FailingDrainAdapter:
        def on_event(self, event: dict) -> None:
            pass

        def on_response(self, response: object) -> None:
            raise RuntimeError("send 400")

        def poll_once(self) -> None:
            pass

    with caplog.at_level(logging.WARNING, logger="imp.daemon"):
        runner._sender_worker(
            _FailingDrainAdapter(), queue.Queue(), responses, stop)
    assert "final response drain failed" in caplog.text


def test_sender_worker_shutdown_survives_a_failing_outbound_drain(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = threading.Event()
    stop.set()                                       # skip the loop, go to final drain
    outbound: queue.Queue = queue.Queue()
    outbound.put({"type": "x"})

    class _FailingOutboundAdapter:
        def on_event(self, event: dict) -> None:
            raise RuntimeError("outbound 400")

        def on_response(self, response: object) -> None:
            pass

        def poll_once(self) -> None:
            pass

    with caplog.at_level(logging.WARNING, logger="imp.daemon"):
        runner._sender_worker(
            _FailingOutboundAdapter(), outbound, queue.Queue(), stop)
    assert "final outbound drain failed" in caplog.text


@pytest.mark.anyio
async def test_reply_to_unknown_change_surfaces_a_user_facing_notice() -> None:
    from imp.engine.errors import LoadError
    from imp.remote.daemon.commands import Notice, Reply
    from imp.remote.daemon.control import ControlPlane
    from imp.remote.daemon.focus import Focus

    async def deliver(_cid: str, _text: str) -> None:
        raise LoadError("no change here")

    control = ControlPlane(
        deliver=deliver,
        create=lambda _p: None,
        abandon=lambda _c: 0,
        list_changes=lambda: [],
        focus=Focus(),
        snapshot=lambda _c: None,
        recent_narrative=lambda _c, _n: [],
    )
    responses: queue.Queue = queue.Queue()
    await runner._handle_command(Reply("2026-06-17-gone", "hi"), control, responses)
    notice = responses.get_nowait()
    assert isinstance(notice, Notice)
    assert "2026-06-17-gone" in notice.text
    assert "unknown or pruned" in notice.text
