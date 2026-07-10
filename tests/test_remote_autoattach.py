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
        quest_id="2026-06-13-abc", title="T", env=env)
    assert dc is None
    assert env == {}


def test_daemon_absent_still_selects_the_connector(tmp_path: Path) -> None:
    # The daemon is the sole gate: a configured driver is ALWAYS run through the
    # connector, which polls for the socket and attaches if/when the daemon comes
    # up. imp never spawns the telegram driver directly, so a missing daemon is a
    # behavioral no-op (the connector polls quietly), not a daemon-less delivery.
    env: dict[str, str] = {}
    dc = select_driver_config(
        _telegram_config(), control_root=tmp_path,
        quest_id="2026-06-13-abc", title="T", env=env)
    assert dc is not None
    assert dc.command[-1] == "imp.remote.drivers.daemon_connector"
    assert env["IMP_REMOTE_CONTROL_SOCK"].endswith("remote-control.sock")
    assert env["IMP_REMOTE_CONTROL_CHANGE_ID"] == "2026-06-13-abc"


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
            quest_id="2026-06-13-abc", title="Dark mode", env=env)
        assert dc is not None
        assert dc.command[-1] == "imp.remote.drivers.daemon_connector"
        assert env["IMP_REMOTE_CONTROL_SOCK"] == str(sock)
        assert env["IMP_REMOTE_CONTROL_CHANGE_ID"] == "2026-06-13-abc"
        assert env["IMP_REMOTE_CONTROL_TITLE"] == "Dark mode"
    finally:
        srv.close()
