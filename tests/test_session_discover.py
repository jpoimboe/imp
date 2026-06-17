from __future__ import annotations

import os
import tempfile
from pathlib import Path

from imp.remote.session.discover import session_socket_path


def test_session_socket_path_is_in_tmpdir_with_sock_extension() -> None:
    root = Path("/tmp/proj")
    path = session_socket_path(root, "2026-06-15-abc")
    expected_dir = Path(tempfile.gettempdir()) / f"imp-{os.getuid()}"
    assert path.parent == expected_dir
    assert path.suffix == ".sock"


def test_session_socket_path_differs_per_change() -> None:
    root = Path("/tmp/proj")
    assert session_socket_path(root, "c1") != session_socket_path(root, "c2")


def test_session_socket_path_differs_per_project() -> None:
    assert session_socket_path(Path("/tmp/a"), "c1") != session_socket_path(Path("/tmp/b"), "c1")
