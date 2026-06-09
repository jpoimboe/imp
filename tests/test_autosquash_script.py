"""autosquash.sh: folds fixup! commits into their targets. When a prior integration
merge is in range it auto-folds POST-merge fixups (rebasing only above the merge, so
the merge — and the integrator's in-merge resolutions — is preserved), and refuses
(leaving the branch pristine) when a fixup targets a PRE-merge commit, which would
require linearizing the merge (review finding #3). Runs the real shipped script
against a real git repo."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

AUTOSQUASH = Path("src/imp/assets/workflows/default/scripts/autosquash.sh").resolve()


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _run(project_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(AUTOSQUASH)],
        cwd=project_root.parent,
        env={**os.environ, "IMP_PROJECT_ROOT": str(project_root)},
        capture_output=True, text=True, check=False,
    )


def _build_through_merge(project_root: Path) -> tuple[str, str]:
    """Common scaffold: build a repo through an integration merge.

    main:    A (gitignore) → B (base v1) → C (base v2)
    imp/x:   A → B → D (branch work) → M (merge --no-ff main@C)

    imp/x is checked out; change.json records base_branch=main.
    Returns (branch_work_sha D, merge_sha M).
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

    # branch: normal commit D
    _git(project_root, "checkout", "-b", "imp/x")
    (project_root / "g.txt").write_text("branch work\n")
    _git(project_root, "add", "g.txt")
    _git(project_root, "commit", "-m", "branch work")
    branch_sha = _git(project_root, "rev-parse", "HEAD").stdout.strip()

    # Advance main: base v2
    _git(project_root, "checkout", "main")
    (project_root / "f.txt").write_text("base v2\n")
    _git(project_root, "add", "f.txt")
    _git(project_root, "commit", "-m", "base v2")

    # Back on imp/x: integration merge M (--no-ff so it's a real merge commit)
    _git(project_root, "checkout", "imp/x")
    _git(project_root, "merge", "--no-ff", "-m", "integrate main into imp/x", "main")
    merge_sha = _git(project_root, "rev-parse", "HEAD").stdout.strip()

    change_dir = project_root / ".imp" / "change"
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "change.json").write_text(json.dumps(
        {"id": "x", "title": "t", "base_branch": "main"}))
    return branch_sha, merge_sha


def _seed_branch_with_integration_merge_and_fixup(
    project_root: Path, *, with_fixup: bool = True
) -> None:
    """Post-integration rework whose fixup targets a PRE-merge commit (the hard
    case): folding it would have to linearize the integration merge."""
    branch_sha, _ = _build_through_merge(project_root)
    if with_fixup:
        # fixup! targets "branch work" (D, below the merge) — not foldable above M.
        (project_root / "g.txt").write_text("branch work (fixed)\n")
        _git(project_root, "add", "g.txt")
        _git(project_root, "commit", f"--fixup={branch_sha}")


def _seed_post_merge_fixup(project_root: Path) -> str:
    """Post-integration rework whose fixup targets a POST-merge commit (the easy
    case): foldable above the merge without touching it.

    imp/x:  … → M (merge) → E (post-merge work) → F (fixup! post-merge work)

    Returns M's sha so the test can assert the merge is preserved.
    """
    _, merge_sha = _build_through_merge(project_root)
    (project_root / "h.txt").write_text("post v1\n")
    _git(project_root, "add", "h.txt")
    _git(project_root, "commit", "-m", "post-merge work")
    post_sha = _git(project_root, "rev-parse", "HEAD").stdout.strip()
    # fixup! targets the post-merge commit E
    (project_root / "h.txt").write_text("post v1 (fixed)\n")
    _git(project_root, "add", "h.txt")
    _git(project_root, "commit", f"--fixup={post_sha}")
    return merge_sha


def _subjects(pr: Path) -> list[str]:
    fork = _git(pr, "merge-base", "main", "HEAD").stdout.strip()
    out = _git(pr, "log", "--format=%s", f"{fork}..HEAD").stdout
    return [s for s in out.splitlines() if s]


def test_autosquash_folds_post_merge_fixup_and_preserves_merge(tmp_path: Path) -> None:
    """The easy case: a fixup! targeting a POST-merge commit is auto-folded by
    rebasing only the segment above the integration merge. The merge commit (and
    the integrator's resolutions baked into it) is left untouched."""
    pr = tmp_path / "wt"
    merge_sha = _seed_post_merge_fixup(pr)
    result = _run(pr)
    assert result.returncode == 0, result.stderr
    # The integration merge is preserved (still an ancestor of the new HEAD)...
    assert _git(pr, "merge-base", "--is-ancestor", merge_sha, "HEAD").returncode == 0
    assert _git(pr, "rev-list", "--merges", "--max-count=1",
                f"{merge_sha}^..HEAD").stdout.strip() == merge_sha
    # ...the fixup was folded (no fixup! remains)...
    assert not any(s.startswith("fixup!") for s in _subjects(pr))
    # ...and the fixup's content landed.
    assert (pr / "h.txt").read_text() == "post v1 (fixed)\n"


def test_autosquash_refuses_pre_merge_fixup_and_leaves_branch_pristine(tmp_path: Path) -> None:
    """The hard case: a fixup! targeting a PRE-merge commit can't be folded above
    the merge. autosquash refuses (engine pauses) AND restores the branch to its
    exact pre-run state — folding it would linearize the merge and drop the
    integrator's in-merge resolutions (review finding #3)."""
    pr = tmp_path / "wt"
    _seed_branch_with_integration_merge_and_fixup(pr)
    head_before = _git(pr, "rev-parse", "HEAD").stdout.strip()
    result = _run(pr)
    assert result.returncode == 1  # refuses -> engine pauses
    # Branch left exactly pristine (any in-flight rebase was rolled back).
    assert _git(pr, "rev-parse", "HEAD").stdout.strip() == head_before
    # The fixup! is still there (nothing was folded or lost).
    assert any(s.startswith("fixup!") for s in _subjects(pr))


def test_autosquash_refuses_pre_merge_squash_directive_pristine(tmp_path: Path) -> None:
    """Parity with the fixup! hard case: a squash!/amend! targeting a PRE-merge
    commit must also be detected as a leftover and refused pristine — not slip
    through as a successful fold. A post-merge fixup! is present so the fast-path
    gate (fixup!-only) is passed and the merge-aware path is exercised."""
    pr = tmp_path / "wt"
    branch_sha, _ = _build_through_merge(pr)
    # post-merge commit E + fixup! → E (foldable above the merge, passes fast path)
    (pr / "h.txt").write_text("post v1\n")
    _git(pr, "add", "h.txt")
    _git(pr, "commit", "-m", "post-merge work")
    post_sha = _git(pr, "rev-parse", "HEAD").stdout.strip()
    (pr / "h.txt").write_text("post v1 (fixed)\n")
    _git(pr, "add", "h.txt")
    _git(pr, "commit", f"--fixup={post_sha}")
    # squash! → D (pre-merge "branch work"): cannot fold above the merge
    (pr / "g.txt").write_text("branch work (more)\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", f"--squash={branch_sha}")
    head_before = _git(pr, "rev-parse", "HEAD").stdout.strip()
    result = _run(pr)
    assert result.returncode == 1, result.stderr
    assert _git(pr, "rev-parse", "HEAD").stdout.strip() == head_before  # pristine
    assert any(s.startswith("squash!") for s in _subjects(pr))  # nothing folded/lost


def test_autosquash_noop_without_fixups_keeps_merge(tmp_path: Path) -> None:
    """Rework-after-integration WITHOUT fixup! commits is the common case.
    autosquash must exit 0 and leave HEAD (including the merge commit) untouched."""
    pr = tmp_path / "wt"
    _seed_branch_with_integration_merge_and_fixup(pr, with_fixup=False)
    head_before = _git(pr, "rev-parse", "HEAD").stdout.strip()
    assert _run(pr).returncode == 0
    assert _git(pr, "rev-parse", "HEAD").stdout.strip() == head_before  # merge untouched
