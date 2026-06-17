"""Session-daemon directory: liveness probes for per-change session sockets."""
from __future__ import annotations

from pathlib import Path

from imp.remote.daemon.discover import socket_is_live
from imp.remote.session.discover import session_socket_path


def session_is_resident(control_root: Path, change_id: str) -> bool:
    """True iff a session daemon is currently accepting on change_id's socket."""
    return socket_is_live(session_socket_path(control_root, change_id))
