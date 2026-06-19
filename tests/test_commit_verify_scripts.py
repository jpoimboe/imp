"""The commit-verify gate scripts: HEAD must touch the expected artifact.
Trailer checks were moved to the review phase (stamp-trailers.sh)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from imp.assets import bundled_workflow_dir

SCRIPTS = bundled_workflow_dir("imp") / "scripts"


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
        env={"IMP_PROJECT_ROOT": str(root), "IMP_CHANGE_HANDLE": slug,
             "PATH": __import__("os").environ["PATH"]},
        capture_output=True, text=True,
    ).returncode


def _run_id(script: str, root: Path, slug: str, change_id: str) -> int:
    return subprocess.run(
        ["bash", str(SCRIPTS / script)], cwd=SCRIPTS,
        env={"IMP_PROJECT_ROOT": str(root), "IMP_CHANGE_HANDLE": slug,
             "IMP_CHANGE_ID": change_id,
             "PATH": __import__("os").environ["PATH"]},
        capture_output=True, text=True,
    ).returncode


def _wip_commit(root: Path, change_id: str, rel: str, body: str) -> None:
    """Write .imp/change/artifacts/<rel> and snapshot to refs/imp/journal-wip/<id>."""
    from imp.engine import worktree
    art = root / ".imp" / "change" / "artifacts"
    dest = art / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body)
    files = {str(p.relative_to(art)): p for p in sorted(art.rglob("*")) if p.is_file()}
    worktree.artifact_commit(root, change_id, files, message="artifact: test")


def test_spec_verify_passes_when_wip_matches_live(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".imp").mkdir()                       # gitignored stage for the temp index
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    _wip_commit(root, "id1", "spec/2026-06-01-x.md", "# spec\n")
    assert _run_id("spec-commit-verify.sh", root, "2026-06-01-x", "id1") == 0


def test_spec_verify_fails_when_wip_ref_absent(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".imp" / "change" / "artifacts" / "spec").mkdir(parents=True)
    (root / ".imp" / "change" / "artifacts" / "spec" / "2026-06-01-x.md").write_text("# spec\n")
    # No artifact_commit -> no wip ref.
    assert _run_id("spec-commit-verify.sh", root, "2026-06-01-x", "id1") != 0


def test_spec_verify_fails_when_blob_absent_from_wip(tmp_path: Path) -> None:
    # Wip ref exists (from a different artifact kind) but carries no spec blob.
    root = _repo(tmp_path)
    (root / ".imp").mkdir()
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    # Commit only a plan artifact — spec/<slug>.md is absent from the wip tree.
    _wip_commit(root, "id1", "plans/2026-06-01-x-plan-1.md", "# plan\n")
    # Live spec file exists but was never committed to the wip tree.
    (root / ".imp" / "change" / "artifacts" / "spec").mkdir(parents=True)
    (root / ".imp" / "change" / "artifacts" / "spec" / "2026-06-01-x.md").write_text("# spec\n")
    assert _run_id("spec-commit-verify.sh", root, "2026-06-01-x", "id1") != 0


def test_spec_verify_fails_when_live_differs_from_committed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".imp").mkdir()
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    _wip_commit(root, "id1", "spec/2026-06-01-x.md", "# spec\n")
    # The agent edited the live file after committing it -> blob mismatch.
    (root / ".imp" / "change" / "artifacts" / "spec" / "2026-06-01-x.md").write_text("# edited\n")
    assert _run_id("spec-commit-verify.sh", root, "2026-06-01-x", "id1") != 0


def test_plan_verify_passes_on_newest_committed_plan(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".imp").mkdir()
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    _wip_commit(root, "id1", "plans/2026-06-01-x-plan-1.md", "# plan 1\n")
    _wip_commit(root, "id1", "plans/2026-06-01-x-plan-2.md", "# plan 2\n")
    assert _run_id("plan-commit-verify.sh", root, "2026-06-01-x", "id1") == 0


def test_plan_verify_fails_when_newest_plan_uncommitted(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".imp").mkdir()
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    _wip_commit(root, "id1", "plans/2026-06-01-x-plan-1.md", "# plan 1\n")
    # A newer plan file exists live but was never committed to the wip ref.
    (root / ".imp" / "change" / "artifacts" / "plans"
     / "2026-06-01-x-plan-2.md").write_text("# plan 2\n")
    assert _run_id("plan-commit-verify.sh", root, "2026-06-01-x", "id1") != 0


def test_plan_verify_passes_after_plan_revision(tmp_path: Path) -> None:
    # A revision is just another wip commit (no fixup machinery): re-snapshot the
    # edited plan and the gate passes off the latest blob.
    root = _repo(tmp_path)
    (root / ".imp").mkdir()
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    _wip_commit(root, "id1", "plans/2026-06-01-x-plan-1.md", "# plan 1\n")
    _wip_commit(root, "id1", "plans/2026-06-01-x-plan-1.md", "# plan 1 (revised)\n")
    assert _run_id("plan-commit-verify.sh", root, "2026-06-01-x", "id1") == 0


def test_plan_verify_fails_when_live_plan_differs_from_committed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".imp").mkdir()
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    _wip_commit(root, "id1", "plans/2026-06-01-x-plan-1.md", "# plan 1\n")
    # Agent edited the live plan after committing it -> blob mismatch.
    (root / ".imp" / "change" / "artifacts" / "plans"
     / "2026-06-01-x-plan-1.md").write_text("# plan 1 edited\n")
    assert _run_id("plan-commit-verify.sh", root, "2026-06-01-x", "id1") != 0


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
        '{"base_branch": "main"}'
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
    # Clean up so tmp_path teardown doesn't fail on a rebase-in-progress worktree.
    subprocess.run(["git", "rebase", "--abort"], cwd=root, capture_output=True)


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


def test_stamp_trailers_fails_on_empty_handle(tmp_path: Path) -> None:
    root = _impl_repo(tmp_path, n_tasks=1)
    _src_commit(root, "f.py", "feat: task 1")
    assert _run("stamp-trailers.sh", root, "") != 0   # ${IMP_CHANGE_HANDLE:?}


def test_stamp_trailers_fails_on_empty_range(tmp_path: Path) -> None:
    # change.json says base=main, but HEAD == fork (no commits since) → the
    # empty-range guard fires: base detection is wrong, not "nothing to stamp".
    root = _repo(tmp_path)
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    cdir = root / ".imp" / "change"
    cdir.mkdir(parents=True)
    (cdir / "change.json").write_text('{"base_branch": "main"}')
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "chore: change metadata")
    # No change branch / no divergence from main beyond what main already has:
    # merge-base(main, HEAD) == HEAD, so the range main..HEAD is empty.
    # (HEAD is already on main, so no force-update needed.)
    assert _run("stamp-trailers.sh", root, "2026-06-01-x") != 0


def _run_id_capture(script: str, root: Path, slug: str, change_id: str):
    return subprocess.run(
        ["bash", str(SCRIPTS / script)], cwd=SCRIPTS,
        env={"IMP_PROJECT_ROOT": str(root), "IMP_CHANGE_HANDLE": slug,
             "IMP_CHANGE_ID": change_id,
             "PATH": __import__("os").environ["PATH"]},
        capture_output=True, text=True,
    )


def test_plan_verify_rejects_plan_over_task_ceiling(tmp_path: Path) -> None:
    # A plan with 13 `## Task` headings (above the ~12 ceiling) is rejected with a
    # "split this" message that resumes plan-writer (non-zero exit).
    root = _repo(tmp_path)
    (root / ".imp").mkdir()
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    body = "# plan 1\n\n" + "".join(
        f"## Task {i}: t{i}\n\nbody\n\n" for i in range(1, 14))   # 13 tasks
    _wip_commit(root, "id1", "plans/2026-06-01-x-plan-1.md", body)
    proc = _run_id_capture("plan-commit-verify.sh", root, "2026-06-01-x", "id1")
    assert proc.returncode != 0
    assert "split" in proc.stderr.lower()
    assert "13" in proc.stderr


def test_plan_verify_passes_plan_within_task_ceiling(tmp_path: Path) -> None:
    # A coherent 8-task plan passes the gate.
    root = _repo(tmp_path)
    (root / ".imp").mkdir()
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    body = "# plan 1\n\n" + "".join(
        f"## Task {i}: t{i}\n\nbody\n\n" for i in range(1, 9))    # 8 tasks
    _wip_commit(root, "id1", "plans/2026-06-01-x-plan-1.md", body)
    proc = _run_id_capture("plan-commit-verify.sh", root, "2026-06-01-x", "id1")
    assert proc.returncode == 0
