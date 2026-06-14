from __future__ import annotations

import contextlib
import io
import os
import socket
import threading
import time
from pathlib import Path

import pytest

import imp.remote.drivers.daemon_connector as connector
from imp.remote.daemon.protocol import parse_hello
from imp.remote.drivers.daemon_connector import main


def _env(path: str) -> dict[str, str]:
    return {
        "IMP_REMOTE_CONTROL_SOCK": path,
        "IMP_REMOTE_CONTROL_CHANGE_ID": "2026-06-13-abc",
        "IMP_REMOTE_CONTROL_TITLE": "Add dark mode",
    }


def test_main_returns_zero_when_socket_absent_and_stdin_eofs(tmp_path: Path) -> None:
    # No daemon → the connector polls. With stdin already at EOF (imp tore us
    # down) the first poll gap sees the EOF and the connector exits cleanly,
    # having delivered nothing.
    out = io.StringIO()
    r_fd, w_fd = os.pipe()
    os.close(w_fd)   # immediate stdin EOF
    with os.fdopen(r_fd) as stdin_pipe:
        rc = main(env=_env(str(tmp_path / "nope.sock")), stdin=stdin_pipe, stdout=out)
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


def test_wait_for_daemon_drains_stdin_then_keeps_polling_until_eof() -> None:
    r_fd, w_fd = os.pipe()
    try:
        os.write(w_fd, b"stale event while the daemon was down\n")
        # Data present → drained and discarded; with no EOF the gap is waited out
        # and polling continues.
        assert connector._wait_for_daemon(r_fd, 0.05) is True
        os.close(w_fd)   # now stdin EOFs
        # EOF → imp tore us down → stop polling.
        assert connector._wait_for_daemon(r_fd, 0.05) is False
    finally:
        os.close(r_fd)


def test_main_attaches_when_the_daemon_appears_and_reattaches_after_a_bounce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The connector starts with the socket ABSENT (no daemon) and must poll until
    # it appears, then re-attach when the daemon bounces. Two incarnations of the
    # server → two hellos.
    monkeypatch.setattr(connector, "_POLL_GAP", 0.02)
    path = str(tmp_path / "rc.sock")
    hellos: list[str] = []

    def serve_once() -> None:
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)   # clear any stale socket file from the prior incarnation
        srv.bind(path)
        srv.listen()
        try:
            conn, _ = srv.accept()
            f = conn.makefile("rwb")
            hellos.append(f.readline().decode().rstrip("\n"))   # hello
            conn.close()
        finally:
            srv.close()

    out = io.StringIO()
    r_fd, w_fd = os.pipe()
    rc_box: list[int] = []

    def run() -> None:
        with os.fdopen(r_fd) as stdin_pipe:
            rc_box.append(main(env=_env(path), stdin=stdin_pipe, stdout=out))

    ct = threading.Thread(target=run)
    ct.start()
    try:
        time.sleep(0.1)            # let the connector poll while the socket is absent
        first = threading.Thread(target=serve_once)
        first.start()
        first.join(timeout=5)
        assert not first.is_alive(), "first daemon incarnation did not finish"

        # The connector's socket dropped (daemon bounced) → it polls again.
        second = threading.Thread(target=serve_once)
        second.start()
        second.join(timeout=5)
        assert not second.is_alive(), "second daemon incarnation did not finish"
    finally:
        os.close(w_fd)            # stdin EOF → connector exits
        ct.join(timeout=5)
    assert not ct.is_alive(), "connector did not exit after stdin EOF"
    assert rc_box == [0]
    assert len(hellos) == 2, "connector did not re-attach after the daemon bounced"
    assert all(parse_hello(h) == ("2026-06-13-abc", "Add dark mode") for h in hellos)
