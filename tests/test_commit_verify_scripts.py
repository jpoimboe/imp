"""The commit-verify gate scripts (spec §12): HEAD must touch the expected
artifact AND carry a `Change: <slug>` trailer. Exercised against a real repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

from imp.assets import default_workflow_dir

SCRIPTS = default_workflow_dir() / "scripts"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


def _run(script: str, root: Path, slug: str) -> int:
    return subprocess.run(
        ["bash", str(SCRIPTS / script)], cwd=SCRIPTS,
        env={"IMP_PROJECT_ROOT": str(root), "IMP_CHANGE_SLUG": slug,
             "PATH": __import__("os").environ["PATH"]},
        capture_output=True, text=True,
    ).returncode


def test_spec_verify_passes_on_trailered_commit(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "imp" / "specs").mkdir(parents=True)
    (root / "imp" / "specs" / "2026-06-01-x.md").write_text("# spec\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "docs: spec\n\nChange: 2026-06-01-x")
    assert _run("spec-commit-verify.sh", root, "2026-06-01-x") == 0


def test_spec_verify_fails_without_trailer(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "imp" / "specs").mkdir(parents=True)
    (root / "imp" / "specs" / "2026-06-01-x.md").write_text("# spec\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "docs: spec (no trailer)")
    assert _run("spec-commit-verify.sh", root, "2026-06-01-x") != 0


def test_spec_verify_fails_when_file_untouched(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "other.txt").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "chore: other\n\nChange: 2026-06-01-x")
    assert _run("spec-commit-verify.sh", root, "2026-06-01-x") != 0


def test_plan_verify_passes_on_newest_plan(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "imp" / "plans").mkdir(parents=True)
    (root / "imp" / "plans" / "2026-06-01-x-plan-1.md").write_text("# plan 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "docs: plan 1\n\nChange: 2026-06-01-x")
    (root / "imp" / "plans" / "2026-06-01-x-plan-2.md").write_text("# plan 2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "docs: plan 2\n\nChange: 2026-06-01-x")
    assert _run("plan-commit-verify.sh", root, "2026-06-01-x") == 0


def test_plan_verify_fails_without_trailer(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "imp" / "plans").mkdir(parents=True)
    (root / "imp" / "plans" / "2026-06-01-x-plan-1.md").write_text("# plan 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "docs: plan 1 (no trailer)")
    assert _run("plan-commit-verify.sh", root, "2026-06-01-x") != 0


def test_plan_verify_fails_when_plan_not_committed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    (root / "imp" / "plans").mkdir(parents=True)
    (root / "imp" / "plans" / "2026-06-01-x-plan-1.md").write_text("# plan 1\n")  # uncommitted
    assert _run("plan-commit-verify.sh", root, "2026-06-01-x") != 0


def test_plan_verify_passes_when_head_is_fixup_of_plan(tmp_path: Path) -> None:
    # Deviation case (HIGH-1): plan revised as a trailer-less fixup of the plan
    # commit, which is now buried/HEAD-fixup. The gate must still pass off the
    # original trailered plan commit.
    root = _repo(tmp_path)
    (root / "imp" / "plans").mkdir(parents=True)
    (root / "imp" / "plans" / "2026-06-01-x-plan-1.md").write_text("# plan 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "docs: add plan 1 for 2026-06-01-x\n\nChange: 2026-06-01-x")
    target = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                            capture_output=True, text=True, check=True).stdout.strip()
    (root / "imp" / "plans" / "2026-06-01-x-plan-1.md").write_text("# plan 1 (revised)\n")
    _git(root, "add", "-A")
    _git(root, "commit", f"--fixup={target}")
    assert _run("plan-commit-verify.sh", root, "2026-06-01-x") == 0


def _impl_repo(tmp_path: Path, *, n_tasks: int = 1) -> Path:
    """Base commit on main, a change branch, and a committed plan with n_tasks
    `## Task` sections. implement-commit-verify reads base_branch from
    .imp/change/change.json and checks base..HEAD; the plan commit touches
    imp/plans (excluded from the source-commit count).
    change.json is committed separately before the plan so it doesn't make the
    plan commit appear as a source commit (it touches paths outside imp/plans)."""
    root = _repo(tmp_path)
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    cdir = root / ".imp" / "change"
    cdir.mkdir(parents=True)
    (cdir / "change.json").write_text(
        '{"base_branch": "main", "slug": "2026-06-01-x"}'
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "chore: init change metadata")
    _git(root, "checkout", "-q", "-b", "imp/2026-06-01-x")
    plans = root / "imp" / "plans"
    plans.mkdir(parents=True)
    body = "".join(f"## Task {i}: t{i}\n\nbody\n\n" for i in range(1, n_tasks + 1))
    (plans / "2026-06-01-x-plan-1.md").write_text(body)
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "docs: plan 1\n\nChange: 2026-06-01-x")
    return root


def _src_commit(root: Path, name: str, msg: str) -> None:
    (root / name).write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", msg)


def test_implement_verify_passes_clean_trailered_one_per_task(tmp_path: Path) -> None:
    root = _impl_repo(tmp_path, n_tasks=1)
    _src_commit(root, "f.py", "feat: task 1\n\nChange: 2026-06-01-x")
    assert _run("implement-commit-verify.sh", root, "2026-06-01-x") == 0


def test_implement_verify_fails_on_dirty_tree(tmp_path: Path) -> None:
    root = _impl_repo(tmp_path, n_tasks=1)
    _src_commit(root, "f.py", "feat: task 1\n\nChange: 2026-06-01-x")
    (root / "f.py").write_text("uncommitted edit\n")     # implementer left work
    assert _run("implement-commit-verify.sh", root, "2026-06-01-x") != 0


def test_implement_verify_fails_on_untrailered_commit(tmp_path: Path) -> None:
    root = _impl_repo(tmp_path, n_tasks=1)
    _src_commit(root, "f.py", "feat: task 1 (bash-committed, no trailer)")
    assert _run("implement-commit-verify.sh", root, "2026-06-01-x") != 0


def test_implement_verify_fails_when_nothing_committed(tmp_path: Path) -> None:
    root = _impl_repo(tmp_path, n_tasks=1)               # no source commit
    assert _run("implement-commit-verify.sh", root, "2026-06-01-x") != 0


def test_implement_verify_fails_on_commit_sprawl(tmp_path: Path) -> None:
    # 1 planned task but 2 source commits → one-commit-per-task violated.
    root = _impl_repo(tmp_path, n_tasks=1)
    _src_commit(root, "a.py", "feat: task 1 part a\n\nChange: 2026-06-01-x")
    _src_commit(root, "b.py", "feat: task 1 part b\n\nChange: 2026-06-01-x")
    assert _run("implement-commit-verify.sh", root, "2026-06-01-x") != 0


def test_implement_verify_ignores_fixup_commits(tmp_path: Path) -> None:
    # A fixup! commit is transient (folded by autosquash) — it must NOT count as a
    # second source commit, nor trip the missing-trailer check.
    root = _impl_repo(tmp_path, n_tasks=1)
    _src_commit(root, "f.py", "feat: task 1\n\nChange: 2026-06-01-x")
    target = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                            capture_output=True, text=True, check=True).stdout.strip()
    (root / "f.py").write_text("fixed\n")
    _git(root, "add", "-A")
    _git(root, "commit", f"--fixup={target}")            # no trailer, fixup! subject
    assert _run("implement-commit-verify.sh", root, "2026-06-01-x") == 0


def test_autosquash_folds_fixups_into_target(tmp_path: Path) -> None:
    root = _impl_repo(tmp_path, n_tasks=1)
    _src_commit(root, "f.py", "feat: task 1\n\nChange: 2026-06-01-x")
    target = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                            capture_output=True, text=True, check=True).stdout.strip()
    (root / "f.py").write_text("v2\n")
    _git(root, "add", "-A")
    _git(root, "commit", f"--fixup={target}")
    assert _run("autosquash.sh", root, "2026-06-01-x") == 0
    subjects = subprocess.run(["git", "log", "--format=%s"], cwd=root,
                              capture_output=True, text=True, check=True).stdout
    assert "fixup!" not in subjects                       # folded
    assert (root / "f.py").read_text() == "v2\n"          # fix applied


def test_autosquash_is_noop_without_fixups(tmp_path: Path) -> None:
    root = _impl_repo(tmp_path, n_tasks=1)
    _src_commit(root, "f.py", "feat: task 1\n\nChange: 2026-06-01-x")
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                            capture_output=True, text=True, check=True).stdout.strip()
    assert _run("autosquash.sh", root, "2026-06-01-x") == 0
    after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                           capture_output=True, text=True, check=True).stdout.strip()
    assert before == after                                # no rebase happened


def test_autosquash_aborts_cleanly_on_conflict(tmp_path: Path) -> None:
    # A fixup of task 1 and a later task 2 both edit the same line → autosquash
    # replay conflicts. The script must `git rebase --abort` and exit non-zero,
    # leaving the branch intact (Resolution 6 → engine pauses for a human).
    root = _impl_repo(tmp_path, n_tasks=1)
    (root / "f.py").write_text("orig\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "feat: task 1\n\nChange: 2026-06-01-x")
    a = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                       capture_output=True, text=True, check=True).stdout.strip()
    (root / "f.py").write_text("later\n")                 # task 2 edits the same line
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "feat: task 2\n\nChange: 2026-06-01-x")
    (root / "f.py").write_text("fixed\n")                 # fixup of task 1, same line
    _git(root, "add", "-A")
    _git(root, "commit", f"--fixup={a}")
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                            capture_output=True, text=True, check=True).stdout.strip()
    assert _run("autosquash.sh", root, "2026-06-01-x") != 0
    after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                           capture_output=True, text=True, check=True).stdout.strip()
    assert before == after                                # aborted, branch intact
    assert not (root / ".git" / "rebase-merge").exists()  # no rebase left in progress
