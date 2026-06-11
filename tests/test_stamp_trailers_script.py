"""stamp-trailers.sh stamps `Change: <slug>` on every non-merge commit in range.

It runs once at the top of the review phase, right after autosquash. On the FIRST
pass the change history is linear (integrate-merge has not run yet), so it stamps
the whole fork-point..HEAD range.

But `final-review` (which runs AFTER integrate-merge) can route `rework -> implement`,
which re-enters the review phase at autosquash -> stamp-trailers. On that second pass
the range already contains the integration merge commit. A plain `git rebase $FORK`
would flatten that merge (dropping its baked-in conflict resolution), so stamp-trailers
must instead stamp only the linear tail ABOVE the most recent integration merge —
mirroring autosquash's cheap path. The pre-merge commits keep their first-pass stamps;
the merge commit is integration plumbing and is left byte-identical."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

STAMP = Path("src/imp/assets/workflows/default/scripts/stamp-trailers.sh").resolve()
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
             "IMP_CHANGE_SLUG": SLUG},
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


def _merges(pr: Path) -> list[str]:
    out = _git(pr, "rev-list", "--merges", f"{_fork(pr)}..HEAD").stdout
    return [s for s in out.splitlines() if s]


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


# ── re-entry: integration merge in range (the bug) ────────────────────────────

def test_stamp_trailers_reentry_stamps_tail_and_preserves_merge(tmp_path: Path) -> None:
    """Second pass after integrate-merge + a post-merge rework. The pre-merge
    commits are already stamped; stamp-trailers must stamp the post-merge tail
    WITHOUT flattening the integration merge or losing its conflict resolution."""
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    (pr / "f.txt").write_text("line1\nline2\nline3\n")
    _git(pr, "add", "f.txt")
    _git(pr, "commit", "-m", "base v1")
    _git(pr, "checkout", "-b", "imp/x")
    (pr / "spec.txt").write_text("spec\n")
    _git(pr, "add", "spec.txt")
    pre1 = _commit(pr, "docs: spec", stamped=True)        # stamped on pass 1
    (pr / "f.txt").write_text("line1\nBRANCH\nline3\n")
    _git(pr, "add", "f.txt")
    pre2 = _commit(pr, "feat: work", stamped=True)        # stamped on pass 1
    # base advances and conflicts on f.txt line 2
    _git(pr, "checkout", "main")
    (pr / "f.txt").write_text("line1\nBASE\nline3\n")
    _git(pr, "add", "f.txt")
    _git(pr, "commit", "-m", "base v2")
    _git(pr, "checkout", "imp/x")
    _git(pr, "merge", "--no-ff", "--no-commit", "main")   # conflict
    (pr / "f.txt").write_text("line1\nBRANCH\nBASE\nline3\n")
    _git(pr, "add", "f.txt")
    _git(pr, "commit", "--no-edit")                       # integration merge M
    merge_sha = _git(pr, "rev-parse", "HEAD").stdout.strip()
    (pr / "h.txt").write_text("post\n")                   # post-merge rework, UNstamped
    _git(pr, "add", "h.txt")
    rework = _commit(pr, "fix: address rework feedback")

    result = _run(pr)
    assert result.returncode == 0, result.stderr
    # The integration merge survives, byte-identical, with its resolution.
    assert _merges(pr) == [merge_sha]
    assert _git(pr, "rev-parse", "HEAD~1").stdout.strip() == merge_sha
    assert (pr / "f.txt").read_text() == "line1\nBRANCH\nBASE\nline3\n"
    # Every non-merge commit carries the trailer.
    assert _change(pr, pre1) == SLUG
    assert _change(pr, pre2) == SLUG
    assert _change(pr, "HEAD") == SLUG                    # rework now stamped
    assert _git(pr, "log", "-1", "--format=%s", "HEAD").stdout.strip() \
        == "fix: address rework feedback"
    # The merge commit itself is integration plumbing — left unstamped.
    assert _change(pr, merge_sha) == ""
    _ = rework  # pre-stamp sha; HEAD is its amended successor
