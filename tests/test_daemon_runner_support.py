from __future__ import annotations

import os
import queue
import socket
from pathlib import Path

import pytest

from imp.remote.daemon import runner
from imp.remote.daemon.discover import socket_path


def test_parse_event_returns_the_dict_for_valid_ndjson() -> None:
    assert runner.parse_event('{"type": "agent_output", "x": 1}') == {
        "type": "agent_output", "x": 1}


def test_parse_event_returns_none_for_garbage() -> None:
    assert runner.parse_event("not json") is None
    assert runner.parse_event("[1, 2, 3]") is None   # valid JSON, but not an event dict


def test_drain_outbound_feeds_each_queued_line_to_on_event() -> None:
    seen: list[dict] = []

    class FakeAdapter:
        def on_event(self, event: dict) -> None:
            seen.append(event)

    q: queue.Queue[str] = queue.Queue()
    q.put('{"type": "a"}')
    q.put("garbage")                 # dropped, not forwarded
    q.put('{"type": "b"}')
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
