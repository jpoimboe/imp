from __future__ import annotations

from pathlib import Path

import pytest

from imp.remote.session.discover import session_socket_path


def test_session_socket_path_is_under_quest_home() -> None:
    path = session_socket_path(Path("/ctl"), "2026-06-15-abc")
    assert path == Path("/ctl/.imp/quests/2026-06-15-abc/session.sock")


def test_session_socket_path_differs_per_quest() -> None:
    root = Path("/ctl")
    assert session_socket_path(root, "c1") != session_socket_path(root, "c2")


def test_session_socket_path_depends_on_control_root() -> None:
    a = session_socket_path(Path("/ctl/a"), "c1")
    b = session_socket_path(Path("/ctl/b"), "c1")
    assert a != b


def test_session_socket_path_over_length_limit_raises() -> None:
    with pytest.raises(ValueError, match="AF_UNIX"):
        session_socket_path(Path("/some/control/root"), "x" * 200)
