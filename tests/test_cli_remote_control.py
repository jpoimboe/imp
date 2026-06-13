from __future__ import annotations

import os
import signal
import socket
from pathlib import Path

from imp.cli.main import (
    _build_parser,
    _cmd_remote_control_start,
    _cmd_remote_control_stop,
)
from imp.remote.daemon import runner
from imp.remote.daemon.discover import socket_path


def test_parser_wires_start_and_stop() -> None:
    parser = _build_parser()
    assert parser.parse_args(
        ["remote-control", "start"]).func is _cmd_remote_control_start
    assert parser.parse_args(
        ["remote-control", "stop"]).func is _cmd_remote_control_stop


def test_start_daemon_rejects_a_second_live_daemon(tmp_path: Path) -> None:
    (tmp_path / ".imp").mkdir()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(socket_path(tmp_path)))
    srv.listen()
    try:
        assert runner.start_daemon(tmp_path) == 2   # single daemon per repo
    finally:
        srv.close()


def test_stop_daemon_reports_when_none_running(tmp_path: Path) -> None:
    (tmp_path / ".imp").mkdir()
    assert runner.stop_daemon(tmp_path) == 1


def test_stop_daemon_signals_the_recorded_pid(tmp_path: Path) -> None:
    (tmp_path / ".imp").mkdir()
    # Record a real, signalable child as the "daemon"; stop must SIGTERM it.
    pid = os.fork()
    if pid == 0:                # child: pause until signalled, then exit
        signal.pause()
        os._exit(0)
    runner.pid_path(tmp_path).write_text(str(pid))
    assert runner.stop_daemon(tmp_path) == 0
    _, status = os.waitpid(pid, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGTERM
