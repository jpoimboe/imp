from __future__ import annotations

import socket
from pathlib import Path

from imp.remote.daemon.directory import session_is_resident
from imp.remote.session.discover import session_socket_path


def test_session_is_resident_tracks_a_live_session_socket(tmp_path: Path) -> None:
    # A stale file is not resident — the predicate probes liveness, not file existence.
    control_root = tmp_path
    change_id = "directory-probe-change"
    path = session_socket_path(control_root, change_id)

    assert session_is_resident(control_root, change_id) is False  # nothing bound yet

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(path))
        srv.listen()
        assert session_is_resident(control_root, change_id) is True

        srv.close()
        # Stale socket file is still on disk, but nothing is accepting → not resident.
        assert path.exists()
        assert session_is_resident(control_root, change_id) is False
    finally:
        srv.close()
        path.unlink(missing_ok=True)
