"""imp-daemon-connector — bridges a Change's NDJSON stdio to the per-repo daemon socket."""
from __future__ import annotations

import contextlib
import os
import select
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


def _pump_socket_to_stdout(sock: socket.socket, stdout: Any) -> None:
    reader = sock.makefile("rb")
    try:
        for raw in reader:
            stdout.write(raw.decode("utf-8", "replace"))
            stdout.flush()
    except (OSError, ValueError):
        pass
    finally:
        with contextlib.suppress(OSError):
            reader.close()


def _pump_stdin_to_sock(
    sock: socket.socket, stdin_fd: int, cancel_r: int
) -> None:
    """Forward stdin lines to sock; exits when stdin closes or cancel_r becomes readable."""
    buf = b""
    try:
        while True:
            r, _, _ = select.select([stdin_fd, cancel_r], [], [])
            if cancel_r in r:
                break
            if stdin_fd not in r:
                continue
            chunk = os.read(stdin_fd, 4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                sock.sendall(line + b"\n")
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_WR)
        with contextlib.suppress(OSError):
            os.close(cancel_r)


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
    cancel_r, cancel_w = os.pipe()
    reader = threading.Thread(
        target=_pump_socket_to_stdout, args=(sock, stdout), daemon=True
    )
    writer = threading.Thread(
        target=_pump_stdin_to_sock, args=(sock, stdin.fileno(), cancel_r), daemon=True
    )
    try:
        sock.sendall((make_hello(change_id, title) + "\n").encode("utf-8"))
        reader.start()
        writer.start()
        reader.join()  # return when socket closes (daemon crash or normal shutdown)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            os.close(cancel_w)  # wake the writer; writer closes cancel_r
        with contextlib.suppress(OSError):
            os.close(cancel_r)  # guard: close if writer never ran or already closed by writer
        with contextlib.suppress(OSError):
            sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
