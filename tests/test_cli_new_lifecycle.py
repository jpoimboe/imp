from pathlib import Path

import pytest

from imp.cli.main import _select_slug
from imp.engine import worktree as wt
from tests.conftest import _seed_control


def test_select_slug_from_title(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    slug, title, auto = _select_slug(tmp_path, title="Add OAuth Support",
                                     today="2026-06-01")
    assert slug == "2026-06-01-add-oauth-support"
    assert title == "Add OAuth Support"
    assert auto is False


def test_select_slug_title_collision_errors(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    # Collision is on the *dated* slug now, so pre-create that exact branch.
    wt.create_worktree(tmp_path, "2026-06-01-add-oauth", "main")
    with pytest.raises(SystemExit):
        _select_slug(tmp_path, title="Add OAuth", today="2026-06-01")


def test_select_slug_random_when_no_title(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    slug, title, auto = _select_slug(tmp_path, title=None, today="2026-06-01")
    assert slug.startswith("2026-06-01-")
    name = slug[len("2026-06-01-"):]
    assert name.count("-") == 2  # three words
    assert title.count(" ") == 2
    assert auto is True
