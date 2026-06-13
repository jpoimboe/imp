from __future__ import annotations

import io
import os
import socket
import threading
from pathlib import Path

from imp.remote.daemon.protocol import parse_hello
from imp.remote.drivers.daemon_connector import main


def _env(path: str) -> dict[str, str]:
    return {
        "IMP_REMOTE_CONTROL_SOCK": path,
        "IMP_REMOTE_CONTROL_CHANGE_ID": "2026-06-13-abc",
        "IMP_REMOTE_CONTROL_TITLE": "Add dark mode",
    }


def test_main_returns_zero_when_socket_absent(tmp_path: Path) -> None:
    out = io.StringIO()
    rc = main(env=_env(str(tmp_path / "nope.sock")), stdin=io.StringIO(""), stdout=out)
    assert rc == 0
    assert out.getvalue() == ""


def test_main_sends_hello_forwards_stdin_and_relays_reply(tmp_path: Path) -> None:
    path = str(tmp_path / "rc.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen()
    received: list[str] = []

    def serve() -> None:
        conn, _ = srv.accept()
        f = conn.makefile("rwb")
        # First line is the hello frame; the second is the forwarded event.
        received.append(f.readline().decode().rstrip("\n"))   # hello
        received.append(f.readline().decode().rstrip("\n"))   # event
        # Push one reply down, then close so the connector's reader sees EOF.
        f.write(b"do the thing\n")
        f.flush()
        conn.close()

    t = threading.Thread(target=serve)
    t.start()
    try:
        out = io.StringIO()
        r_fd, w_fd = os.pipe()
        event_line = b'{"type": "agent_output", "payload": {"k": "v"}}\n'
        os.write(w_fd, event_line)
        os.close(w_fd)   # EOF after the one event line
        with os.fdopen(r_fd) as stdin_pipe:
            rc = main(env=_env(path), stdin=stdin_pipe, stdout=out)
        t.join(timeout=5)
        assert not t.is_alive(), "server thread did not finish in time"
        assert rc == 0
        assert out.getvalue() == "do the thing\n"
        assert parse_hello(received[0]) == ("2026-06-13-abc", "Add dark mode")
        assert received[1] == '{"type": "agent_output", "payload": {"k": "v"}}'
    finally:
        srv.close()
