from __future__ import annotations

from pathlib import Path

from imp.remote.session.discover import session_socket_path


def test_session_socket_path_is_per_change_under_imp_sessions() -> None:
    root = Path("/tmp/proj")
    assert session_socket_path(root, "2026-06-15-abc") == (
        root / ".imp" / "sessions" / "2026-06-15-abc.sock"
    )


def test_session_socket_path_differs_per_change() -> None:
    root = Path("/tmp/proj")
    assert session_socket_path(root, "c1") != session_socket_path(root, "c2")
