"""`imp init` detects up front that git can build no author identity."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.cli.init import _git_can_commit
from tests.conftest import _git


@pytest.fixture
def isolated_git_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Cut git off from the developer's config files and from any identity in the
    environment. The global path is a nonexistent file rather than /dev/null so a
    stray `git config --global` write stays observable."""
    global_config = tmp_path / "global-gitconfig"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(var, raising=False)
    return global_config


@pytest.fixture
def broken_repo(tmp_path: Path, isolated_git_config: Path) -> Path:
    """A repo where git can resolve no author identity at all: no readable config,
    nothing in the environment, and GECOS/$EMAIL auto-detection disabled."""
    root = tmp_path / "broken"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.useConfigOnly", "true")
    return root


@pytest.fixture
def ordinary_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The everyday case: no repo-local identity, but the environment supplies one."""
    root = tmp_path / "ordinary"
    root.mkdir()
    _git(root, "init", "-q")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    return root


def test_probe_true_on_an_ordinary_repo(ordinary_repo: Path) -> None:
    assert _git_can_commit(ordinary_repo) is True


def test_probe_false_when_git_can_resolve_no_identity(broken_repo: Path) -> None:
    # The fixture only means anything if git really refuses to commit here.
    (broken_repo / "f.txt").write_text("x\n")
    _git(broken_repo, "add", "f.txt")
    assert _git(broken_repo, "commit", "-m", "x").returncode != 0

    assert _git_can_commit(broken_repo) is False


def test_probe_true_when_email_env_supplies_the_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_git_config: Path,
) -> None:
    root = tmp_path / "email-only"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Ada Lovelace")
    monkeypatch.setenv("EMAIL", "ada@example.org")
    # `git config --get user.email` reads only config files, so the wrong probe
    # would call this repo unconfigured even though `git commit` works fine.
    assert _git(root, "config", "--get", "user.email").returncode != 0

    assert _git_can_commit(root) is True
