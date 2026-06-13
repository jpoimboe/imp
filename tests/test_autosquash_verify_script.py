"""autosquash-verify.sh is the objective gate after autosquash-resolve.

A fixup! fold is tree-preserving, so a correct resolution reproduces the
pre-autosquash tree exactly. The verify script asserts: HEAD^{tree} == the recorded
BEFORE_TREE, no fixup!/squash!/amend! subjects remain, and no rebase is in progress.
It owns a 3-attempt bound and, on exhaustion, restores the branch to the
pre-autosquash commit ITSELF before pausing (the engine's max_loops cannot)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

AUTOSQUASH = Path("src/imp/assets/workflows/default/scripts/autosquash.sh").resolve()
# autosquash-verify.sh folded INTO autosquash.sh: a present before-tree sentinel
# selects verify-mode, so the verify-behavior fixtures drive autosquash.sh itself.
VERIFY = AUTOSQUASH


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=False, env={**os.environ, "GIT_EDITOR": "true"})


def _run(script: Path, pr: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)], cwd=pr.parent,
        env={**os.environ, "IMP_PROJECT_ROOT": str(pr)},
        capture_output=True, text=True, check=False,
    )


def _init(pr: Path) -> None:
    pr.mkdir(parents=True, exist_ok=True)
    _git(pr, "init", "-b", "main")
    _git(pr, "config", "user.email", "t@t")
    _git(pr, "config", "user.name", "t")
    (pr / ".gitignore").write_text(".imp/\n")
    _git(pr, "add", ".gitignore")
    _git(pr, "commit", "-m", "gitignore .imp/")


def _record_change(pr: Path) -> None:
    change_dir = pr / ".imp" / "change"
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "change.json").write_text(json.dumps(
        {"id": "x", "title": "t", "base_branch": "main"}))


def _build_and_handoff(pr: Path) -> tuple[str, str]:
    """Build the plain-path conflict, run autosquash.sh to the exit-2 hand-off, and
    return (before_head, before_tree) captured before the rebase started."""
    _init(pr)
    _record_change(pr)
    _git(pr, "checkout", "-b", "imp/x")
    (pr / "a.txt").write_text("orig\n")
    _git(pr, "add", "a.txt")
    _git(pr, "commit", "-m", "target")
    target = _git(pr, "rev-parse", "HEAD").stdout.strip()
    (pr / "a.txt").write_text("intervened\n")
    _git(pr, "add", "a.txt")
    _git(pr, "commit", "-m", "intervening")
    (pr / "a.txt").write_text("fixed\n")
    _git(pr, "add", "a.txt")
    _git(pr, "commit", f"--fixup={target}")
    before_head = _git(pr, "rev-parse", "HEAD").stdout.strip()
    before_tree = _git(pr, "rev-parse", "HEAD^{tree}").stdout.strip()
    handoff = _run(AUTOSQUASH, pr)
    assert handoff.returncode == 2, handoff.stderr  # precondition: rebase mid-flight
    return before_head, before_tree


def _resolve(pr: Path, content: str) -> None:
    """Drive the mid-flight rebase to completion, resolving every stop by writing
    `content` to a.txt (the only conflicted path in this fixture)."""
    while True:
        rebase_dir = _git(pr, "rev-parse", "--git-path", "rebase-merge").stdout.strip()
        if not (pr / rebase_dir).is_dir():
            break
        (pr / "a.txt").write_text(content)
        _git(pr, "add", "a.txt")
        _git(pr, "rebase", "--continue")


def _state(pr: Path) -> Path:
    return pr / ".imp" / "change" / "state"


def test_verify_passes_correct_resolution(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _, before_tree = _build_and_handoff(pr)
    _resolve(pr, "fixed\n")  # reproduces the pre-autosquash tree

    result = _run(VERIFY, pr)
    assert result.returncode == 0, result.stderr
    assert _git(pr, "rev-parse", "HEAD^{tree}").stdout.strip() == before_tree
    subjects = _git(pr, "log", "--format=%s",
                    f'{_git(pr, "merge-base", "main", "HEAD").stdout.strip()}..HEAD').stdout
    assert not any(s.startswith(("fixup!", "squash!", "amend!"))
                   for s in subjects.splitlines())
    assert not (_state(pr) / "autosquash-before-tree").exists()   # sentinels cleaned
    assert not (_state(pr) / "autosquash-resolve-attempts").exists()
    assert not (_state(pr) / "autosquash-before-head").exists()


def test_verify_rejects_rebase_in_progress(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _build_and_handoff(pr)
    # rebase is still mid-flight; do NOT call _resolve

    result = _run(VERIFY, pr)
    assert result.returncode == 2, result.stderr
    assert "in progress" in result.stderr.lower()
    assert (_state(pr) / "autosquash-resolve-attempts").read_text().strip() == "1"
    assert (_state(pr) / "autosquash-before-tree").exists()  # sentinels kept for retry
    assert (_state(pr) / "autosquash-before-head").exists()


def test_verify_rejects_leftover_fixup_subject(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    _git(pr, "checkout", "-b", "imp/x")
    # A fixup! commit whose target doesn't exist — autosquash would leave it in place.
    (pr / "a.txt").write_text("base\n")
    _git(pr, "add", "a.txt")
    _git(pr, "commit", "-m", "base commit")
    (pr / "a.txt").write_text("changed\n")
    _git(pr, "add", "a.txt")
    _git(pr, "commit", "-m", "fixup! nonexistent target")  # leftover fixup! subject
    # Record BEFORE_TREE as the current HEAD tree (so tree check passes, leftover check fires)
    before_tree = _git(pr, "rev-parse", "HEAD^{tree}").stdout.strip()
    before_head = _git(pr, "rev-parse", "HEAD").stdout.strip()
    _state(pr).mkdir(parents=True, exist_ok=True)
    (_state(pr) / "autosquash-before-tree").write_text(before_tree + "\n")
    (_state(pr) / "autosquash-before-head").write_text(before_head + "\n")

    result = _run(VERIFY, pr)
    assert result.returncode == 2, result.stderr
    assert "fixup" in result.stderr.lower() or "squash" in result.stderr.lower() or "amend" in result.stderr.lower()
    assert (_state(pr) / "autosquash-resolve-attempts").read_text().strip() == "1"
    assert (_state(pr) / "autosquash-before-tree").exists()


def test_verify_rejects_drift_and_bounces(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _build_and_handoff(pr)
    _resolve(pr, "WRONG\n")  # drops the fix: tree diverges from BEFORE_TREE

    result = _run(VERIFY, pr)
    assert result.returncode == 2, result.stderr
    assert "tree" in result.stderr.lower()
    assert (_state(pr) / "autosquash-resolve-attempts").read_text().strip() == "1"
    assert (_state(pr) / "autosquash-before-tree").exists()  # kept for the next attempt


def test_verify_exhaustion_restores_pristine_itself(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    before_head, before_tree = _build_and_handoff(pr)
    _resolve(pr, "WRONG\n")
    (_state(pr) / "autosquash-resolve-attempts").write_text("2")  # this run is #3 of 3

    result = _run(VERIFY, pr)
    assert result.returncode == 1, result.stderr
    assert _git(pr, "rev-parse", "HEAD").stdout.strip() == before_head      # HEAD pristine
    assert _git(pr, "rev-parse", "HEAD^{tree}").stdout.strip() == before_tree
    assert not (_state(pr) / "autosquash-before-tree").exists()
    assert not (_state(pr) / "autosquash-resolve-attempts").exists()
    assert not (_state(pr) / "autosquash-before-head").exists()
    assert "rebase -i --autosquash" in result.stderr  # manual-recovery message
