from __future__ import annotations

import socket
from pathlib import Path

from imp.remote.drivers.daemon_connector import dial


def test_dial_returns_none_when_socket_absent(tmp_path: Path) -> None:
    assert dial(str(tmp_path / "nope.sock")) is None


def test_dial_connects_to_a_live_socket(tmp_path: Path) -> None:
    path = str(tmp_path / "rc.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen()
    try:
        conn = dial(path)
        assert conn is not None
        try:
            assert conn.family == socket.AF_UNIX
            conn.getpeername()  # raises OSError if not actually connected
        finally:
            conn.close()
    finally:
        srv.close()
