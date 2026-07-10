from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from imp.remote.daemon.directory import resident_session_ids, session_is_resident
from imp.remote.session.discover import session_socket_path


def test_session_is_resident_tracks_a_live_session_socket(tmp_path: Path) -> None:
    # A stale file is not resident — the predicate probes liveness, not file existence.
    control_root = tmp_path
    quest_id = "directory-probe-change"
    path = session_socket_path(control_root, quest_id)

    assert session_is_resident(control_root, quest_id) is False  # nothing bound yet

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(path))
        srv.listen()
        assert session_is_resident(control_root, quest_id) is True

        srv.close()
        # Stale socket file is still on disk, but nothing is accepting → not resident.
        assert path.exists()
        assert session_is_resident(control_root, quest_id) is False
    finally:
        srv.close()
        path.unlink(missing_ok=True)


def _make_home(control_root: Path, quest_id: str) -> Path:
    home = control_root / ".imp" / "changes" / quest_id
    home.mkdir(parents=True, exist_ok=True)
    (home / "change.json").write_text(json.dumps({"title": quest_id}))
    return home


def _bind_live_socket(control_root: Path, quest_id: str) -> socket.socket:
    path = session_socket_path(control_root, quest_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen()
    return srv


def test_resident_session_ids_returns_only_changes_with_a_live_session(
    tmp_path: Path,
) -> None:
    # Two live in-flight homes on disk; only one has a resident session daemon
    # (a live socket). A hibernated in-flight home (no live socket) is NOT
    # resident, and no session is invented for a home that has none.
    control_root = tmp_path
    _make_home(control_root, "live-one")
    _make_home(control_root, "hibernated-one")

    srv = _bind_live_socket(control_root, "live-one")
    try:
        assert resident_session_ids(control_root) == {"live-one"}
    finally:
        srv.close()
        session_socket_path(control_root, "live-one").unlink(missing_ok=True)


def test_resident_session_ids_never_enumerates_the_archive(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    # archived_ids is the sole archive gateway; guarding it uncalled proves zero event-blob reads.
    control_root = tmp_path
    _make_home(control_root, "live-one")

    def _boom(_cr: Path) -> list[str]:
        raise AssertionError("residency poll must not enumerate the archive")

    monkeypatch.setattr("imp.cli.main.archive_read.archived_ids", _boom)

    srv = _bind_live_socket(control_root, "live-one")
    try:
        assert resident_session_ids(control_root) == {"live-one"}
    finally:
        srv.close()
        session_socket_path(control_root, "live-one").unlink(missing_ok=True)
