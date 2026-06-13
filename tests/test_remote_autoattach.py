from __future__ import annotations

import socket
from pathlib import Path
from types import SimpleNamespace

from imp.cli.remote_channel import select_driver_config


def _telegram_config() -> SimpleNamespace:
    return SimpleNamespace(remote={"driver": {"name": "telegram"}})


def test_no_remote_config_returns_none(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    dc = select_driver_config(
        SimpleNamespace(remote={}), control_root=tmp_path,
        change_id="2026-06-13-abc", title="T", env=env)
    assert dc is None
    assert env == {}


def test_daemon_absent_keeps_the_configured_driver(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    dc = select_driver_config(
        _telegram_config(), control_root=tmp_path,
        change_id="2026-06-13-abc", title="T", env=env)
    assert dc is not None
    assert dc.command[-1] == "imp.remote.drivers.telegram"
    assert "IMP_REMOTE_CONTROL_SOCK" not in env   # no attach env injected


def test_live_socket_selects_the_connector_and_sets_env(tmp_path: Path) -> None:
    (tmp_path / ".imp").mkdir()
    sock = tmp_path / ".imp" / "remote-control.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock))
    srv.listen()
    try:
        env: dict[str, str] = {}
        dc = select_driver_config(
            _telegram_config(), control_root=tmp_path,
            change_id="2026-06-13-abc", title="Dark mode", env=env)
        assert dc is not None
        assert dc.command[-1] == "imp.remote.drivers.daemon_connector"
        assert env["IMP_REMOTE_CONTROL_SOCK"] == str(sock)
        assert env["IMP_REMOTE_CONTROL_CHANGE_ID"] == "2026-06-13-abc"
        assert env["IMP_REMOTE_CONTROL_TITLE"] == "Dark mode"
    finally:
        srv.close()
