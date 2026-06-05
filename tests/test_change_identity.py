"""Identity is immutable; slug derives from title (spec §1, AC 1)."""

from __future__ import annotations

from pathlib import Path

from imp.engine.change import Change
from tests.conftest import _seed_control


def test_title_change_moves_slug_not_id_or_paths(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="First Name",
                       change_id="2026-06-04-k3f9qa", base_branch="main")
    id_before = ch.id
    root_before = ch.project_root
    slug_before = ch.slug
    assert slug_before == "2026-06-04-first-name"

    ch.title = "Second Name"
    assert ch.slug == "2026-06-04-second-name"     # slug follows title
    assert ch.slug != slug_before
    assert ch.id == id_before                       # id unchanged
    assert ch.project_root == root_before           # worktree path unchanged
    # Branch name is imp/<id>, independent of title.
    from tests.conftest import _git
    assert "imp/2026-06-04-k3f9qa" in _git(tmp_path, "branch", "--list",
                                           "imp/2026-06-04-k3f9qa").stdout
