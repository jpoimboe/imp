"""Identity is immutable; handle derives from title (spec §1, AC 1)."""

from __future__ import annotations

from pathlib import Path

from imp.engine.change import Change
from tests.conftest import _seed_control


def test_title_change_moves_handle_not_id_or_paths(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="First Name",
                       change_id="k3f9qa", base_branch="main")
    id_before = ch.id
    root_before = ch.project_root
    handle_before = ch.handle
    assert handle_before == "first-name-k3f9qa"

    ch.title = "Second Name"
    assert ch.handle == "second-name-k3f9qa"     # handle follows title
    assert ch.handle != handle_before
    assert ch.id == id_before                       # id unchanged
    assert ch.project_root == root_before           # worktree path unchanged
    # Branch name is imp/<id>, independent of title.
    from tests.conftest import _git
    assert "imp/k3f9qa" in _git(tmp_path, "branch", "--list", "imp/k3f9qa").stdout
