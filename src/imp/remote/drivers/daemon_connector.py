"""imp-daemon-connector — bridges a Change's NDJSON stdio to the per-repo daemon socket.

The connector owns daemon liveness. It polls for the socket, attaches whenever the
daemon is up, and re-attaches across daemon restarts. While the daemon is down it
sits in a quiet poll loop and delivers nothing — so a run with a configured driver
but no daemon is a behavioral no-op (the daemon is the sole gate for remote
activity). The connector stops when imp tears it down: stdin EOF, or SIGTERM at
RemoteChannel teardown.
"""
from __future__ import annotations

import contextlib
import os
import select
import socket
import sys
import threading
import time
from collections.abc import Mapping
from typing import Any

from imp.remote.daemon.protocol import make_hello

_POLL_GAP = 1.0   # seconds between dial attempts while the daemon is down


def dial(path: str) -> socket.socket | None:
    """Connect to the Unix socket at path; return the socket, or None on failure."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
    except OSError:
        sock.close()
        return None
    return sock


def _log(msg: str) -> None:
    """Diagnostics go to stderr; stdout is reserved for reply lines. Unprefixed
    lines land in imp's `imp.remote` logger at DEBUG (default-silent) — see
    remote_channel._classify_stderr_line."""
    print(f"imp-daemon-connector: {msg}", file=sys.stderr, flush=True)


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
    sock: socket.socket, stdin_fd: int, cancel_r: int, stdin_eof: threading.Event
) -> None:
    """Forward stdin lines to sock; exits when stdin closes or cancel_r becomes
    readable. Sets `stdin_eof` only on a real stdin close (not on cancel) so the
    caller can tell "imp tore us down" (stop) from "daemon dropped" (re-attach)."""
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
                stdin_eof.set()
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


def _wait_for_daemon(stdin_fd: int, gap: float) -> bool:
    """Wait out one polling gap while the daemon is down. Returns True to keep
    polling, or False if stdin reached EOF (imp tore us down). Any stdin bytes
    that arrive during the gap are drained and discarded so a reconnect starts
    clean — events emitted while the daemon was down are dropped, never replayed
    stale."""
    end = time.monotonic() + gap
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return True
        r, _, _ = select.select([stdin_fd], [], [], remaining)
        if stdin_fd not in r:
            return True
        if not os.read(stdin_fd, 4096):   # b"" → stdin EOF
            return False
        # data arrived while down → discard and keep waiting out the gap


def _wants_autosubscribe(env: Mapping[str, str]) -> bool:
    return env.get("IMP_REMOTE_AUTOSUBSCRIBE") == "1"


def _bridge(
    sock: socket.socket, stdin_fd: int, quest_id: str, title: str, stdout: Any,
    autosubscribe: bool = False,
) -> bool:
    """Bridge one live connection until the socket drops or stdin EOFs. Returns
    True iff stdin reached EOF (imp gone → stop); False if the socket dropped
    (daemon gone → re-dial). Mirrors the historic one-shot bridge, made reusable."""
    cancel_r, cancel_w = os.pipe()
    stdin_eof = threading.Event()
    reader = threading.Thread(
        target=_pump_socket_to_stdout, args=(sock, stdout), daemon=True
    )
    writer = threading.Thread(
        target=_pump_stdin_to_sock,
        args=(sock, stdin_fd, cancel_r, stdin_eof), daemon=True,
    )
    try:
        sock.sendall((make_hello(quest_id, title, autosubscribe) + "\n").encode("utf-8"))
        reader.start()
        writer.start()
        reader.join()  # return when socket closes (daemon drop or normal shutdown)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            os.close(cancel_w)  # wake the writer; writer closes cancel_r
        if writer.is_alive():
            writer.join()
        with contextlib.suppress(OSError):
            os.close(cancel_r)  # guard: close if writer never ran or already closed
        with contextlib.suppress(OSError):
            sock.close()
    return stdin_eof.is_set()


def main(
    env: Mapping[str, str] | None = None,
    stdin: Any = None,
    stdout: Any = None,
) -> int:
    """Poll for the daemon socket, attach whenever it is up, and re-attach across
    restarts. Forward stdin NDJSON up and relay replies down while attached; sit
    quietly polling while it is down. Returns 0 when imp tears us down (stdin EOF;
    SIGTERM at teardown kills the process outright)."""
    env = os.environ if env is None else env
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout

    path = env.get("IMP_REMOTE_CONTROL_SOCK")
    if not path:
        return 0
    quest_id = env.get("IMP_REMOTE_CONTROL_CHANGE_ID", "")
    title = env.get("IMP_REMOTE_CONTROL_TITLE", "")
    autosubscribe = _wants_autosubscribe(env)
    stdin_fd = stdin.fileno()

    waiting_logged = False
    while True:
        sock = dial(path)
        if sock is not None:
            waiting_logged = False
            _log("attached to daemon socket")
            if _bridge(sock, stdin_fd, quest_id, title, stdout, autosubscribe):
                return 0   # stdin EOF during the bridge: imp tore us down
            # socket dropped (daemon stop/crash) → fall through to the poll gap
        elif not waiting_logged:
            _log("waiting for daemon socket")   # edge-triggered: once per down-stretch
            waiting_logged = True
        if not _wait_for_daemon(stdin_fd, _POLL_GAP):
            return 0   # stdin EOF: imp tore us down


if __name__ == "__main__":
    raise SystemExit(main())
