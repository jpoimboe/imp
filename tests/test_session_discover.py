from __future__ import annotations

import os
import tempfile
from pathlib import Path

from imp.engine.quest import Quest
from imp.remote.session.discover import session_socket_path
from tests.conftest import _seed_control


def test_session_socket_path_is_per_quest_tmp_subdir() -> None:
    path = session_socket_path(Path("/tmp/proj"), "2026-06-15-abc")
    expected = (Path(tempfile.gettempdir()) / f"imp-{os.getuid()}"
                / "2026-06-15-abc" / "session.sock")
    assert path == expected


def test_session_socket_path_differs_per_quest() -> None:
    root = Path("/tmp/proj")
    assert session_socket_path(root, "c1") != session_socket_path(root, "c2")


def test_session_socket_path_is_control_root_independent() -> None:
    a = session_socket_path(Path("/tmp/a"), "c1")
    b = session_socket_path(Path("/tmp/b"), "c1")
    assert a == b


def test_session_socket_path_equals_quest_tmp_dir_join(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="T", quest_id="sockq1", base_branch="main")
    assert session_socket_path(ch.control_root, ch.id) == ch.tmp_dir / "session.sock"
