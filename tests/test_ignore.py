from __future__ import annotations

import subprocess
from pathlib import Path

import anyio

from imp.agents.tools.ignore import (
    is_ignored,
    list_unignored,
    partition_ignored,
    repo_root,
)


def _init_git_repo(root: Path, gitignore: str = "build/\n*.log\n.mypy_cache/\n") -> None:
    def g(*a: str) -> None:
        subprocess.run(["git", *a], cwd=root, capture_output=True,
                       text=True, check=True)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (root / ".gitignore").write_text(gitignore)
    (root / "README.md").write_text("seed\n")
    g("add", "-A")
    g("commit", "-qm", "seed")


def test_repo_root_finds_toplevel_from_subdir(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    sub = tmp_path / "src"
    sub.mkdir()
    result = anyio.run(repo_root, sub)
    assert result is not None
    assert result.samefile(tmp_path)


def test_repo_root_none_outside_repo(tmp_path: Path) -> None:
    assert anyio.run(repo_root, tmp_path) is None


def test_is_ignored_true_for_ignored_false_for_tracked(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "build").mkdir()
    assert anyio.run(is_ignored, tmp_path, tmp_path / "build") is True
    assert anyio.run(is_ignored, tmp_path, tmp_path / "README.md") is False


def test_list_unignored_keeps_tracked_and_untracked_drops_ignored(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "tracked.py").write_text("x")
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "add tracked"], cwd=tmp_path,
                   capture_output=True, text=True, check=True)
    (tmp_path / "fresh.py").write_text("x")          # untracked, not ignored
    (tmp_path / "skip.log").write_text("x")          # ignored by *.log
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.o").write_text("x")   # ignored by build/

    result = anyio.run(list_unignored, tmp_path, tmp_path)
    assert result is not None
    names = {p.name for p in result}
    assert "tracked.py" in names
    assert "fresh.py" in names
    assert "skip.log" not in names
    assert "out.o" not in names


def test_list_unignored_honors_nested_gitignore(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".gitignore").write_text("*.tmp\n")
    (sub / "keep.py").write_text("x")
    (sub / "drop.tmp").write_text("x")

    result = anyio.run(list_unignored, tmp_path, tmp_path)
    assert result is not None
    names = {p.name for p in result}
    assert "keep.py" in names
    assert "drop.tmp" not in names


def test_list_unignored_returns_absolute_paths(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "a.py").write_text("x")
    result = anyio.run(list_unignored, tmp_path, tmp_path)
    assert result is not None
    assert all(p.is_absolute() for p in result)
    assert (tmp_path / "a.py").resolve() in {p.resolve() for p in result}


def test_list_unignored_none_when_not_a_repo(tmp_path: Path) -> None:
    assert anyio.run(list_unignored, tmp_path, tmp_path) is None


def test_partition_ignored_returns_only_ignored_subset(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "build").mkdir()
    ign = tmp_path / "build" / "o"
    ign.write_text("x")
    keep = tmp_path / "keep.py"
    keep.write_text("x")

    result = anyio.run(partition_ignored, tmp_path, {keep, ign})
    assert result == {ign}


def test_partition_ignored_empty_input_returns_empty_set(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    assert anyio.run(partition_ignored, tmp_path, set()) == set()


def test_partition_ignored_none_when_not_a_repo(tmp_path: Path) -> None:
    assert anyio.run(partition_ignored, tmp_path, {tmp_path / "x"}) is None
