from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from imp.cli.init import IntegrationBranchUnresolved, _resolve_init_base


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")


def _init_repo(root: Path, branch: str) -> None:
    subprocess.run(["git", "init", "-q", "-b", branch, str(root)], check=True)


def _commit(root: Path) -> None:
    subprocess.run(
        ["git", "-C", str(root), "commit", "--allow-empty", "-qm", "seed"],
        check=True,
    )


def _no_prompt(_p: str) -> str:
    raise AssertionError("must not prompt")


def test_resolve_prefers_origin_head(tmp_path: Path) -> None:
    root = tmp_path / "r"
    _init_repo(root, "feature")
    subprocess.run(
        ["git", "-C", str(root), "symbolic-ref",
         "refs/remotes/origin/HEAD", "refs/remotes/origin/mainline"],
        check=True,
    )
    assert _resolve_init_base(
        root, git_created=False, input_fn=_no_prompt) == "mainline"


def test_resolve_unborn_uses_current_without_prompt(tmp_path: Path) -> None:
    root = tmp_path / "r"
    _init_repo(root, "trunk")  # unborn: no commits yet
    assert _resolve_init_base(
        root, git_created=False, input_fn=_no_prompt) == "trunk"


def test_resolve_git_created_uses_current_without_prompt(tmp_path: Path) -> None:
    root = tmp_path / "r"
    _init_repo(root, "main")
    _commit(root)  # has commits, but git_created short-circuits the prompt
    assert _resolve_init_base(
        root, git_created=True, input_fn=_no_prompt) == "main"


def test_resolve_prompts_and_takes_typed_answer(tmp_path: Path) -> None:
    root = tmp_path / "r"
    _init_repo(root, "main")
    _commit(root)
    prompts: list[str] = []

    def _input(p: str) -> str:
        prompts.append(p)
        return "release"

    assert _resolve_init_base(
        root, git_created=False, input_fn=_input) == "release"
    assert prompts and "integrate into" in prompts[0]


def test_resolve_prompt_enter_accepts_current(tmp_path: Path) -> None:
    root = tmp_path / "r"
    _init_repo(root, "main")
    _commit(root)
    assert _resolve_init_base(
        root, git_created=False, input_fn=lambda _p: "") == "main"


def test_resolve_eof_raises(tmp_path: Path) -> None:
    root = tmp_path / "r"
    _init_repo(root, "main")
    _commit(root)

    def _input(_p: str) -> str:
        raise EOFError

    with pytest.raises(IntegrationBranchUnresolved):
        _resolve_init_base(root, git_created=False, input_fn=_input)


def test_resolve_detached_head_empty_answer_raises(tmp_path: Path) -> None:
    root = tmp_path / "r"
    _init_repo(root, "main")
    _commit(root)
    subprocess.run(
        ["git", "-C", str(root), "checkout", "--detach", "-q"], check=True)
    # No remote default, has commits, no current branch -> prompt with no
    # default; an empty answer has nothing to fall back on, so it raises.
    with pytest.raises(IntegrationBranchUnresolved):
        _resolve_init_base(root, git_created=False, input_fn=lambda _p: "")
