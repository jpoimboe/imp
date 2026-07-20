from __future__ import annotations

import os
import tempfile
from pathlib import Path

from imp.engine.quest import Quest
from imp.remote.session.discover import session_socket_path
from tests.conftest import _seed_control


def test_tmp_dir_is_per_quest_subdir_of_imp_uid(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="T", quest_id="qtmp1", base_branch="main")
    assert ch.tmp_dir.name == "qtmp1"
    assert ch.tmp_dir.parent.name == f"imp-{os.getuid()}"
    assert ch.tmp_dir.parent.parent == Path(tempfile.gettempdir())


def test_quest_tmp_root_is_isolated_per_test() -> None:
    # The autouse fixture redirects the tmp root to a short unique per-test dir,
    # so session sockets never share the raw system-temp /tmp/imp-<uid> root.
    tmpdir = tempfile.gettempdir()
    assert "imp-t-" in tmpdir
    assert Path(tmpdir).is_dir()
    sock = session_socket_path(Path("/any/control"), "some-id")
    assert str(sock).startswith(tmpdir)
