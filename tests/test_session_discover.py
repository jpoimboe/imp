from __future__ import annotations

import os
import tempfile
from pathlib import Path

from imp.remote.session.discover import session_socket_path


def test_session_socket_path_is_tmp_sibling() -> None:
    path = session_socket_path("2026-06-15-abc")
    assert path == (
        Path(tempfile.gettempdir()) / f"imp-{os.getuid()}" / "2026-06-15-abc.sock")


def test_session_socket_path_differs_per_quest() -> None:
    assert session_socket_path("c1") != session_socket_path("c2")


def test_session_socket_path_is_sibling_of_quest_tmp_leaf() -> None:
    sock = session_socket_path("c1")
    assert sock.parent == Path(tempfile.gettempdir()) / f"imp-{os.getuid()}"
    assert sock.name == "c1.sock"
