from __future__ import annotations

import json
from pathlib import Path

from imp.engine.change import LAYOUT_VERSION, Change, _imp_commit
from tests.conftest import _git, _seed_control


def test_layout_version_constant_is_five() -> None:
    assert LAYOUT_VERSION == 5


def test_create_stamps_version_and_imp_commit(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    ch = Change.create(tmp_path, title="X", change_id="lv1", base_branch="main")
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert meta["layout_version"] == 5
    assert meta["imp_commit"] == head


def test_create_draft_stamps_version_and_imp_commit(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    ch = Change.create_draft(tmp_path, title="X", change_id="lv2", base_branch="main")
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert meta["layout_version"] == 5
    assert meta["imp_commit"] == head


def test_imp_commit_is_empty_when_not_a_git_repo(tmp_path: Path) -> None:
    # tmp_path is not a git repository: git rev-parse HEAD fails, helper yields "".
    assert _imp_commit(tmp_path) == ""
