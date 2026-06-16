"""Walk-up project root discovery (marker: imp/project.yaml)."""

from __future__ import annotations

from pathlib import Path

from imp.engine.project_root import find_project_root


def test_finds_imp_dir_in_cwd(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    assert find_project_root(start=tmp_path) == tmp_path


def test_walks_up_from_subdir(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert find_project_root(start=deep) == tmp_path


def test_returns_cwd_when_no_imp_dir(tmp_path: Path) -> None:
    # `imp init` is expected to run before any imp/project.yaml exists.
    assert find_project_root(start=tmp_path) == tmp_path


def test_terminates_at_filesystem_root() -> None:
    """Walking up from / must return / without looping. Verifies the
    cur.parent == cur termination."""
    root = Path("/")
    # In practice / has no imp/project.yaml, but even if it did the test
    # would still validate termination. We just need this call to return,
    # not hang.
    result = find_project_root(start=root)
    # Whether it returns root or some ancestor doesn't matter — only that
    # it returned at all (no infinite loop).
    assert isinstance(result, Path)


def test_old_config_yaml_marker_no_longer_matches(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "imp").mkdir(parents=True)
    (root / "imp" / "config.yaml").write_text("providers: [openai]\n")
    sub = root / "sub"
    sub.mkdir()
    # No project.yaml anywhere → "no project yet": returns the start dir (sub),
    # NOT root. (Old code, treating config.yaml as a marker, would return root.)
    assert find_project_root(sub) == sub
