from __future__ import annotations

import socket
from pathlib import Path

from imp.remote.daemon.discover import socket_is_live, socket_path


def test_socket_path_is_under_control_imp_dir(tmp_path: Path) -> None:
    assert socket_path(tmp_path) == tmp_path / ".imp" / "remote-control.sock"


def test_socket_is_live_false_when_absent(tmp_path: Path) -> None:
    assert socket_is_live(tmp_path / ".imp" / "remote-control.sock") is False


def test_socket_is_live_false_for_a_stray_socket_with_no_listener(tmp_path: Path) -> None:
    # A bound-then-closed socket leaves the file on disk but nothing listening:
    # connecting is refused, so it must NOT count as live (spec: a stray socket
    # never pulls in an imp).
    path = tmp_path / "rc.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.close()
    assert socket_is_live(path) is False


def test_socket_is_live_true_when_a_listener_accepts(tmp_path: Path) -> None:
    path = tmp_path / "rc.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen()
    try:
        assert socket_is_live(path) is True
    finally:
        srv.close()
