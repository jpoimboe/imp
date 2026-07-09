from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from imp.cli.init import GitSetupDeclined, run_init


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    monkeypatch.setattr("imp.cli.init._default_detect_theme", lambda: "ansi")


def test_decline_aborts_before_picker_and_writes_nothing(tmp_path: Path) -> None:
    prompts: list[str] = []
    picked: list[object] = []

    def _input(prompt: str = "") -> str:
        prompts.append(prompt)
        return "n"

    def _picker(available: list[str]) -> list[str]:
        picked.append(available)
        return ["anthropic"]

    with pytest.raises(GitSetupDeclined):
        run_init(tmp_path, input_fn=_input, output_fn=lambda _m: None,
                 picker_fn=_picker)

    assert any("Initialize a git repository" in p for p in prompts)
    assert picked == []                                  # picker never ran
    assert not (tmp_path / ".git").exists()              # no repo created
    assert not (tmp_path / "imp" / "project.yaml").exists()
    assert not (tmp_path / ".gitignore").exists()


def test_default_yes_initializes_git(tmp_path: Path) -> None:
    run_init(tmp_path, input_fn=lambda _p: "", output_fn=lambda _m: None,
             picker_fn=lambda _a: ["anthropic"], detect_verify_fn=lambda _p: None)
    assert (tmp_path / ".git").is_dir()
    assert (tmp_path / "imp" / "project.yaml").is_file()


def test_existing_repo_skips_the_prompt(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    prompts: list[str] = []
    run_init(tmp_path,
             input_fn=lambda p: prompts.append(p) or "",
             output_fn=lambda _m: None, picker_fn=lambda _a: ["anthropic"],
             detect_verify_fn=lambda _p: None)
    assert not any("Initialize a git repository" in p for p in prompts)


def test_ensure_git_repo_reports_creation(tmp_path: Path) -> None:
    from imp.cli.init import _ensure_git_repo

    created = _ensure_git_repo(
        tmp_path, input_fn=lambda _p: "", output_fn=lambda _m: None)
    assert created is True                       # this call ran `git init`
    assert (tmp_path / ".git").is_dir()

    again = _ensure_git_repo(
        tmp_path, input_fn=lambda _p: "", output_fn=lambda _m: None)
    assert again is False                        # repo already present
