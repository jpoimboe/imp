"""Walk-up project root discovery (marker: imp/config.yaml)."""

from __future__ import annotations

from pathlib import Path

from imp.engine.project_root import find_project_root


def test_finds_imp_dir_in_cwd(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("roles: {}\n")
    assert find_project_root(start=tmp_path) == tmp_path


def test_walks_up_from_subdir(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text("roles: {}\n")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert find_project_root(start=deep) == tmp_path


def test_returns_cwd_when_no_imp_dir(tmp_path: Path) -> None:
    # `imp init` is expected to run before any imp/config.yaml exists.
    assert find_project_root(start=tmp_path) == tmp_path


def test_terminates_at_filesystem_root() -> None:
    """Walking up from / must return / without looping. Verifies the
    cur.parent == cur termination."""
    root = Path("/")
    # In practice / has no imp/config.yaml, but even if it did the test
    # would still validate termination. We just need this call to return,
    # not hang.
    result = find_project_root(start=root)
    # Whether it returns root or some ancestor doesn't matter — only that
    # it returned at all (no infinite loop).
    assert isinstance(result, Path)
