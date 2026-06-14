"""The commit-verify gate scripts: HEAD must touch the expected artifact.
Trailer checks were moved to the review phase (stamp-trailers.sh)."""

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


def test_spec_verify_passes_on_commit_touching_spec(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "imp" / "specs").mkdir(parents=True)
    (root / "imp" / "specs" / "2026-06-01-x.md").write_text("# spec\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "docs: spec")        # no Change: trailer
    assert _run("spec-commit-verify.sh", root, "2026-06-01-x") == 0


def test_spec_verify_fails_when_file_untouched(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "other.txt").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "chore: other\n\nChange: 2026-06-01-x")
    assert _run("spec-commit-verify.sh", root, "2026-06-01-x") != 0


def test_spec_verify_fails_on_uncommitted_spec_edit(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "imp" / "specs").mkdir(parents=True)
    (root / "imp" / "specs" / "2026-06-01-x.md").write_text("# spec\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "docs: spec")
    # HEAD touches the spec (old check passes) but the working tree has an
    # uncommitted edit to it -> new clean-spec-path check must fail.
    (root / "imp" / "specs" / "2026-06-01-x.md").write_text("# spec edited\n")
    assert _run("spec-commit-verify.sh", root, "2026-06-01-x") != 0


def test_spec_verify_fails_on_untracked_stray_in_specs(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "imp" / "specs").mkdir(parents=True)
    (root / "imp" / "specs" / "2026-06-01-x.md").write_text("# spec\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "docs: spec")
    # An untracked scratch file under imp/specs/ -> porcelain is non-empty -> fail.
    (root / "imp" / "specs" / "scratch.md").write_text("draft\n")
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


def test_implement_verify_passes_on_clean_tree(tmp_path: Path) -> None:
    root = _impl_repo(tmp_path, n_tasks=1)
    _src_commit(root, "f.py", "feat: task 1")        # no trailer
    _src_commit(root, "g.py", "feat: task 1 more")   # 2 commits, no count cap now
    assert _run("implement-commit-verify.sh", root, "2026-06-01-x") == 0


def test_implement_verify_fails_on_dirty_tree(tmp_path: Path) -> None:
    root = _impl_repo(tmp_path, n_tasks=1)
    _src_commit(root, "f.py", "feat: task 1\n\nChange: 2026-06-01-x")
    (root / "f.py").write_text("uncommitted edit\n")     # implementer left work
    assert _run("implement-commit-verify.sh", root, "2026-06-01-x") != 0


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


def test_autosquash_hands_off_conflict_to_resolve(tmp_path: Path) -> None:
    # A fixup of task 1 and a later task 2 both edit the same line → the autosquash
    # fold conflicts. The collapsed script no longer aborts on a plain-path fold
    # conflict; it leaves the rebase in progress and exits 2 to hand off to
    # autosquash-resolve (the workflow routes exit 2 → autosquash-resolve), writing
    # the before-tree/before-head sentinels the verify re-entry checks against.
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
    assert _run("autosquash.sh", root, "2026-06-01-x") == 2   # hand-off, not abort
    assert (root / ".git" / "rebase-merge").exists()          # rebase left in progress
    state = root / ".imp" / "change" / "state"
    assert (state / "autosquash-before-tree").exists()        # sentinels for verify-mode
    assert (state / "autosquash-before-head").exists()


def _change_subject(root: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%(trailers:key=Change,valueonly)", ref],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_stamp_trailers_stamps_every_commit(tmp_path: Path) -> None:
    root = _impl_repo(tmp_path, n_tasks=1)            # base, change.json, plan
    _src_commit(root, "f.py", "feat: task 1")        # no trailer
    _src_commit(root, "g.py", "feat: task 1 more")   # no trailer
    assert _run("stamp-trailers.sh", root, "2026-06-01-x") == 0
    # Every commit since the fork now carries the trailer.
    revs = subprocess.run(
        ["git", "rev-list", "main..HEAD"], cwd=root,
        capture_output=True, text=True, check=True,
    ).stdout.split()
    for h in revs:
        assert _change_subject(root, h) == "2026-06-01-x", f"{h} not stamped"


def test_stamp_trailers_is_idempotent(tmp_path: Path) -> None:
    root = _impl_repo(tmp_path, n_tasks=1)
    _src_commit(root, "f.py", "feat: task 1")
    assert _run("stamp-trailers.sh", root, "2026-06-01-x") == 0
    assert _run("stamp-trailers.sh", root, "2026-06-01-x") == 0
    body = subprocess.run(
        ["git", "log", "-1", "--format=%B", "HEAD"], cwd=root,
        capture_output=True, text=True, check=True,
    ).stdout
    assert body.count("Change: 2026-06-01-x") == 1   # not duplicated


def test_stamp_trailers_fails_on_empty_slug(tmp_path: Path) -> None:
    root = _impl_repo(tmp_path, n_tasks=1)
    _src_commit(root, "f.py", "feat: task 1")
    assert _run("stamp-trailers.sh", root, "") != 0   # ${IMP_CHANGE_SLUG:?}


def test_stamp_trailers_fails_on_empty_range(tmp_path: Path) -> None:
    # change.json says base=main, but HEAD == fork (no commits since) → the
    # empty-range guard fires: base detection is wrong, not "nothing to stamp".
    root = _repo(tmp_path)
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    cdir = root / ".imp" / "change"
    cdir.mkdir(parents=True)
    (cdir / "change.json").write_text('{"base_branch": "main", "slug": "2026-06-01-x"}')
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "chore: change metadata")
    # No change branch / no divergence from main beyond what main already has:
    # merge-base(main, HEAD) == HEAD, so the range main..HEAD is empty.
    # (HEAD is already on main, so no force-update needed.)
    assert _run("stamp-trailers.sh", root, "2026-06-01-x") != 0
