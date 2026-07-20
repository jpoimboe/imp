from __future__ import annotations

import os
import tempfile
from pathlib import Path

from imp.engine.quest import Quest
from imp.remote.session.attach import ensure_quest_tmp_dir
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


def test_ensure_quest_tmp_dir_creates_0o700_parent_and_leaf(tmp_path: Path) -> None:
    # The autouse _isolate_quest_tmp_root fixture already points the tmp root at a
    # short unique per-test dir, so the created tree is hermetic and auto-cleaned.
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="T", quest_id="ensq1", base_branch="main")

    ensure_quest_tmp_dir(ch)

    leaf = ch.tmp_dir
    assert leaf.is_dir()
    assert leaf.parent.is_dir()
    assert leaf.stat().st_mode & 0o777 == 0o700
    assert leaf.parent.stat().st_mode & 0o777 == 0o700

    # Pre-existing dirs at the wrong mode must be corrected, and a repeat call
    # (resume-after-reap / live-daemon-already-holds-dir) must stay idempotent.
    leaf.chmod(0o755)
    leaf.parent.chmod(0o755)
    ensure_quest_tmp_dir(ch)
    assert leaf.is_dir()
    assert leaf.stat().st_mode & 0o777 == 0o700
    assert leaf.parent.stat().st_mode & 0o777 == 0o700
