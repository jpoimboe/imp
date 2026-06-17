"""stamp-trailers.sh stamps `Change: <slug>` on every non-merge commit in range.

It runs at the top of the review phase, right after autosquash. The branch is always
linear (integrate-rebase produces a linear result), so it stamps the whole
fork-point..HEAD range on every pass."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

STAMP = Path("src/imp/assets/workflows/imp/scripts/stamp-trailers.sh").resolve()
SLUG = "2026-06-11-stamp-test"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=False,
                          env={**os.environ, "GIT_EDITOR": "true"})


def _run(project_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(STAMP)],
        cwd=project_root.parent,
        env={**os.environ, "IMP_PROJECT_ROOT": str(project_root),
             "IMP_CHANGE_HANDLE": SLUG},
        capture_output=True, text=True, check=False,
    )


def _init(project_root: Path) -> None:
    project_root.mkdir(parents=True, exist_ok=True)
    _git(project_root, "init", "-b", "main")
    _git(project_root, "config", "user.email", "t@t")
    _git(project_root, "config", "user.name", "t")
    # git-ignore .imp/ BEFORE writing there (mirrors production; keeps the tree
    # porcelain-clean for the rebase).
    (project_root / ".gitignore").write_text(".imp/\n")
    _git(project_root, "add", ".gitignore")
    _git(project_root, "commit", "-m", "gitignore .imp/")


def _record_change(project_root: Path) -> None:
    change_dir = project_root / ".imp" / "change"
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "change.json").write_text(json.dumps(
        {"id": "x", "title": "t", "base_branch": "main"}))


def _commit(pr: Path, msg: str, *, stamped: bool = False) -> str:
    body = msg if not stamped else f"{msg}\n\nChange: {SLUG}"
    _git(pr, "commit", "-m", body)
    return _git(pr, "rev-parse", "HEAD").stdout.strip()


def _change(pr: Path, h: str) -> str:
    return _git(pr, "log", "-1", "--format=%(trailers:key=Change,valueonly)", h
                ).stdout.strip()


def _fork(pr: Path) -> str:
    return _git(pr, "merge-base", "main", "HEAD").stdout.strip()



# ── first pass: linear history ────────────────────────────────────────────────

def test_stamp_trailers_stamps_linear_range(tmp_path: Path) -> None:
    """First review pass — no integration merge yet. Every commit gets the trailer."""
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    _git(pr, "checkout", "-b", "imp/x")
    (pr / "spec.txt").write_text("spec\n")
    _git(pr, "add", "spec.txt")
    _commit(pr, "docs: spec")
    (pr / "g.txt").write_text("work\n")
    _git(pr, "add", "g.txt")
    _commit(pr, "feat: work")

    result = _run(pr)
    assert result.returncode == 0, result.stderr
    # Stamping rebases the range, so reference the rewritten commits by position.
    assert _change(pr, "HEAD~1") == SLUG   # docs: spec
    assert _change(pr, "HEAD") == SLUG     # feat: work

