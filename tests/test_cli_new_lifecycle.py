from pathlib import Path

import pytest

from imp.cli.main import _select_slug
from imp.engine import worktree as wt
from tests.conftest import _seed_control


def test_select_slug_from_title(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    slug, title = _select_slug(tmp_path, title="Add OAuth Support")
    assert slug == "add-oauth-support" and title == "Add OAuth Support"


def test_select_slug_title_collision_errors(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    wt.create_worktree(tmp_path, "add-oauth", "main")
    with pytest.raises(SystemExit):
        _select_slug(tmp_path, title="Add OAuth")


def test_select_slug_random_when_no_title(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    slug, title = _select_slug(tmp_path, title=None)
    assert slug.count("-") == 2  # three words
    assert title.count(" ") == 2
