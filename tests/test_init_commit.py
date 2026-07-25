from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from imp.cli.init import run_init


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    monkeypatch.setattr("imp.cli.init._default_detect_theme", lambda: "ansi")
    import imp.cli.init as initmod
    from imp.agents.interactive import InteractiveSuccess
    monkeypatch.setattr(initmod, "sandbox_available", lambda: True, raising=False)
    monkeypatch.setattr(initmod, "resolve_detection_provider",
                        lambda _p, **_kw: ("anthropic", "m"), raising=False)
    monkeypatch.setattr(initmod, "_run_init_conversation", lambda **kw: InteractiveSuccess({
        "status": "Success", "git_init": True, "default_workflow": "default",
        "default_base": "main", "test_cmd_fast": "", "pythonpath": {"roots": []}}),
        raising=False)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True,
                          text=True, check=True).stdout


def _run(root: Path) -> None:
    run_init(root, input_fn=lambda _p: "", output_fn=lambda _m: None,
             picker_fn=lambda _a: ["anthropic"])


def test_creates_scoped_initial_commit(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _run(tmp_path)
    assert _git(tmp_path, "log", "-1", "--format=%s").strip() == (
        "imp: initialize project")
    tracked = _git(tmp_path, "ls-tree", "-r", "--name-only", "HEAD").split()
    assert "imp/project.yaml" in tracked
    assert ".gitignore" in tracked


def test_commit_excludes_unrelated_staged_file(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "other.txt").write_text("x\n")
    subprocess.run(["git", "add", "--", "other.txt"], cwd=tmp_path, check=True)
    _run(tmp_path)
    tracked = _git(tmp_path, "ls-tree", "-r", "--name-only", "HEAD").split()
    assert "other.txt" not in tracked              # not swept into imp's commit
    staged = _git(tmp_path, "diff", "--cached", "--name-only").split()
    assert "other.txt" in staged                   # left staged, untouched


def test_idempotent_rerun_makes_no_new_commit(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _run(tmp_path)
    head1 = _git(tmp_path, "rev-parse", "HEAD").strip()
    _run(tmp_path)
    head2 = _git(tmp_path, "rev-parse", "HEAD").strip()
    assert head1 == head2


def test_commit_includes_memory_scaffolds(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _run(tmp_path)
    tracked = _git(tmp_path, "ls-tree", "-r", "--name-only", "HEAD").split()
    assert "imp/memory/all.md" in tracked
    assert "imp/memory/default/all.md" in tracked
