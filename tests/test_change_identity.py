"""Identity is immutable; the title is free-form (spec §1, AC 1)."""

from __future__ import annotations

from pathlib import Path

from imp.engine.change import Change
from tests.conftest import _git, _seed_control


def test_title_change_keeps_id_and_paths(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="First Name",
                       change_id="k3f9qa", base_branch="main")
    id_before = ch.id
    root_before = ch.project_root

    ch.title = "Second Name"
    assert ch.id == id_before                       # id never changes
    assert ch.project_root == root_before           # worktree path unchanged
    # Branch name is imp/<id>, independent of title.
    assert "imp/k3f9qa" in _git(tmp_path, "branch", "--list", "imp/k3f9qa").stdout
