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
