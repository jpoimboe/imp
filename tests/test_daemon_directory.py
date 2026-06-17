from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

from imp.remote.daemon.directory import resident_session_ids, session_is_resident
from imp.remote.daemon.discover import ChangeInfo
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


def test_resident_session_ids_returns_only_changes_with_a_live_session(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    # Two in-flight changes are known to the per-repo daemon, but only one has a
    # resident session daemon (a live socket). The directory returns that one only —
    # a hibernated in-flight change is NOT resident.
    control_root = tmp_path
    infos = [
        ChangeInfo(id="live-one", title="t1", status="running", step="plan"),
        ChangeInfo(id="hibernated-one", title="t2", status="paused", step="plan"),
    ]
    monkeypatch.setattr(
        "imp.remote.daemon.directory.list_changes", lambda _cr: infos)

    live_path = session_socket_path(control_root, "live-one")
    live_path.parent.mkdir(parents=True, exist_ok=True)
    if live_path.exists():
        live_path.unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(live_path))
        srv.listen()
        assert resident_session_ids(control_root) == {"live-one"}
    finally:
        srv.close()
        live_path.unlink(missing_ok=True)
