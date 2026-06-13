"""imp-daemon-connector — bridges a Change's NDJSON stdio to the per-repo daemon socket."""
from __future__ import annotations

import socket


def dial(path: str) -> socket.socket | None:
    """Connect to the Unix socket at path; return the socket, or None on failure."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
    except OSError:
        sock.close()
        return None
    return sock
