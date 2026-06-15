from __future__ import annotations

import os
import socket
import threading

from imp.remote.daemon.protocol import parse_hello
from imp.remote.drivers.daemon_connector import _bridge


class _StubStdout:
    def write(self, _s: str) -> None: ...
    def flush(self) -> None: ...


def _capture_hello(autosubscribe: bool) -> tuple[str, str, bool]:
    srv, cli = socket.socketpair()
    received: list[bytes] = []

    def serve() -> None:
        reader = srv.makefile("rb")
        line = reader.readline()
        received.append(line)
        srv.close()

    t = threading.Thread(target=serve)
    t.start()
    r_fd, w_fd = os.pipe()          # an immediately-EOF stdin so _bridge returns
    os.close(w_fd)
    _bridge(cli, r_fd, "2026-06-14-abc", "Add dark mode", _StubStdout(),
            autosubscribe=autosubscribe)
    t.join()
    return parse_hello(received[0].decode("utf-8").strip())


def test_bridge_sends_autosubscribe_true_when_requested() -> None:
    assert _capture_hello(True) == ("2026-06-14-abc", "Add dark mode", True)


def test_bridge_sends_autosubscribe_false_by_default() -> None:
    assert _capture_hello(False) == ("2026-06-14-abc", "Add dark mode", False)


def test_wants_autosubscribe_parses_env_correctly() -> None:
    from imp.remote.drivers import daemon_connector
    assert daemon_connector._wants_autosubscribe({"IMP_REMOTE_AUTOSUBSCRIBE": "1"}) is True
    assert daemon_connector._wants_autosubscribe({}) is False
    assert daemon_connector._wants_autosubscribe(
        {"IMP_REMOTE_AUTOSUBSCRIBE": "0"}) is False
    assert daemon_connector._wants_autosubscribe({"IMP_REMOTE_AUTOSUBSCRIBE": "true"}) is False
