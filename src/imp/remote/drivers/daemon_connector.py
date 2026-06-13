"""imp-daemon-connector — bridges a Change's NDJSON stdio to the per-repo daemon socket."""
from __future__ import annotations

import contextlib
import os
import socket
import sys
import threading
from collections.abc import Mapping
from typing import Any

from imp.remote.daemon.protocol import make_hello


def dial(path: str) -> socket.socket | None:
    """Connect to the Unix socket at path; return the socket, or None on failure."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
    except OSError:
        sock.close()
        return None
    return sock


_JOIN_TIMEOUT = 5.0


def _pump_socket_to_stdout(sock: socket.socket, stdout: Any) -> None:
    reader = sock.makefile("rb")
    try:
        for raw in reader:
            stdout.write(raw.decode("utf-8", "replace"))
            stdout.flush()
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            reader.close()


def main(
    env: Mapping[str, str] | None = None,
    stdin: Any = None,
    stdout: Any = None,
) -> int:
    """Bridge entry: dial the daemon socket, forward stdin NDJSON up, relay replies down."""
    env = os.environ if env is None else env
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout

    path = env.get("IMP_REMOTE_CONTROL_SOCK")
    if not path:
        return 0
    sock = dial(path)
    if sock is None:
        return 0

    change_id = env.get("IMP_REMOTE_CONTROL_CHANGE_ID", "")
    title = env.get("IMP_REMOTE_CONTROL_TITLE", "")
    reader = threading.Thread(
        target=_pump_socket_to_stdout, args=(sock, stdout), daemon=True
    )
    reader_started = False
    try:
        sock.sendall((make_hello(change_id, title) + "\n").encode("utf-8"))
        reader.start()
        reader_started = True
        for line in stdin:
            if not line.endswith("\n"):
                line += "\n"
            sock.sendall(line.encode("utf-8"))
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_WR)
        if reader_started:
            reader.join(timeout=_JOIN_TIMEOUT)
        with contextlib.suppress(OSError):
            sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
