"""autosquash.sh: folds fixup! commits into their targets. Guards against silently
linearizing a prior integration merge commit (review finding #3). Runs the real
shipped script against a real git repo."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

AUTOSQUASH = Path("src/imp/assets/workflows/default/scripts/autosquash.sh").resolve()


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _seed_branch_with_integration_merge_and_fixup(
    project_root: Path, *, with_fixup: bool = True
) -> None:
    """Build a repo that mirrors a post-integration rework scenario.

    main:    A (gitignore) → B (base v1) → C (base v2)
    imp/x:   A → B → D (branch work) → M (merge --no-ff main@C)
                                         [→ E (fixup! branch work)]  if with_fixup

    imp/x is checked out; change.json records base_branch=main.
    """
    project_root.mkdir(parents=True, exist_ok=True)
    _git(project_root, "init", "-b", "main")
    _git(project_root, "config", "user.email", "t@t")
    _git(project_root, "config", "user.name", "t")
    # MANDATORY: commit a .gitignore covering .imp/ BEFORE anything writes there.
    # Mirrors production (worktree.py requires .imp/ git-ignored). Without this,
    # .imp/ surfaces as untracked dirt that breaks clean-porcelain assumptions in
    # other tests that share the filesystem.
    (project_root / ".gitignore").write_text(".imp/\n")
    _git(project_root, "add", ".gitignore")
    _git(project_root, "commit", "-m", "gitignore .imp/")

    # base v1
    (project_root / "f.txt").write_text("base v1\n")
    _git(project_root, "add", "f.txt")
    _git(project_root, "commit", "-m", "base v1")

    # branch: normal commit
    _git(project_root, "checkout", "-b", "imp/x")
    (project_root / "g.txt").write_text("branch work\n")
    _git(project_root, "add", "g.txt")
    _git(project_root, "commit", "-m", "branch work")
    # Capture the SHA of the branch-work commit for fixup targeting
    branch_sha = _git(project_root, "rev-parse", "HEAD").stdout.strip()

    # Advance main: base v2
    _git(project_root, "checkout", "main")
    (project_root / "f.txt").write_text("base v2\n")
    _git(project_root, "add", "f.txt")
    _git(project_root, "commit", "-m", "base v2")

    # Back on imp/x: create the integration merge (--no-ff so it's a real merge commit)
    _git(project_root, "checkout", "imp/x")
    _git(project_root, "merge", "--no-ff", "-m", "integrate main into imp/x", "main")

    if with_fixup:
        # Add a fixup! commit that targets "branch work" — git --autosquash will
        # want to squash this into the earlier commit, which would require rebasing
        # over (and thus linearizing) the integration merge commit.
        (project_root / "g.txt").write_text("branch work (fixed)\n")
        _git(project_root, "add", "g.txt")
        # Use --fixup=<sha> so git generates the correct "fixup! branch work" subject
        _git(project_root, "commit", f"--fixup={branch_sha}")

    # Write change.json
    change_dir = project_root / ".imp" / "change"
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "change.json").write_text(json.dumps(
        {"id": "x", "title": "t", "base_branch": "main"}))


def test_autosquash_refuses_when_merge_commit_in_range_with_fixups(tmp_path: Path) -> None:
    """A rework-after-integration with fixup! commits must be refused: squashing
    would linearize the integration merge and silently drop the integrator's
    in-merge conflict resolutions (review finding #3). Refusal causes the engine
    to pause rather than destroy that work."""
    pr = tmp_path / "wt"
    _seed_branch_with_integration_merge_and_fixup(pr)
    rc = subprocess.run(
        ["bash", str(AUTOSQUASH)],
        cwd=pr.parent,
        env={**os.environ, "IMP_PROJECT_ROOT": str(pr)},
        capture_output=True, text=True, check=False,
    ).returncode
    assert rc == 1  # refuses -> engine pauses; merge commit NOT linearized


def test_autosquash_noop_without_fixups_keeps_merge(tmp_path: Path) -> None:
    """Rework-after-integration WITHOUT fixup! commits is the common case.
    autosquash must exit 0 and leave HEAD (including the merge commit) untouched."""
    pr = tmp_path / "wt"
    _seed_branch_with_integration_merge_and_fixup(pr, with_fixup=False)
    head_before = _git(pr, "rev-parse", "HEAD").stdout.strip()
    rc = subprocess.run(
        ["bash", str(AUTOSQUASH)],
        cwd=pr.parent,
        env={**os.environ, "IMP_PROJECT_ROOT": str(pr)},
        capture_output=True, text=True, check=False,
    ).returncode
    assert rc == 0
    assert _git(pr, "rev-parse", "HEAD").stdout.strip() == head_before  # merge untouched
