from __future__ import annotations

import os
import signal
import socket
import unittest.mock
from pathlib import Path
from typing import ClassVar

import pytest

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


def test_start_daemon_refuses_without_remote_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    # A control root with no remote.driver configured must refuse rc 2 and
    # never reach anyio.run (no socket bound, no blind spawns).
    (tmp_path / ".imp").mkdir()

    def _must_not_run(*_a: object, **_kw: object) -> None:
        raise AssertionError("daemon started despite missing remote.driver")

    monkeypatch.setattr(runner.anyio, "run", _must_not_run)
    assert runner.start_daemon(tmp_path) == 2
    assert "remote.driver" in capsys.readouterr().err


def test_start_daemon_refuses_with_unrecognised_driver_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # A config with a remote.driver block but an unregistered name must refuse
    # with rc 2 and a message mentioning "not recognised".
    from imp.remote.daemon import runner

    class _FakeConfig:
        remote: ClassVar[dict] = {"driver": {"name": "no-such-driver"}}

    def _fake_load(**_: object) -> object:
        class R:
            config = _FakeConfig()
        return R()

    with unittest.mock.patch(
        "imp.providers.effective.load_effective_config", side_effect=_fake_load
    ), unittest.mock.patch(
        "imp.remote.daemon.runner.check_not_running"
    ):
        assert runner.start_daemon(tmp_path) == 2
    assert "not recognised" in capsys.readouterr().err


def test_start_daemon_returns_rc2_for_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.remote.drivers.telegram import ConfigError

    (tmp_path / ".imp").mkdir()
    (tmp_path / ".imp" / "config.yaml").write_text(
        "remote:\n  driver:\n    name: telegram\n")

    def _raise_config_error(*_a: object, **_kw: object) -> None:
        raise ConfigError("no token configured")

    monkeypatch.setattr(runner.anyio, "run", _raise_config_error)
    assert runner.start_daemon(tmp_path) == 2
