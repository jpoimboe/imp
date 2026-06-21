"""The artifact commit-verify gate scripts (spec/plan): the live artifact must
match the blob committed to the change's wip lineage."""

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


def _wip_commit(root: Path, change_id: str, rel: str, body: str) -> None:
    """Write .imp/change/artifacts/<rel> and snapshot to refs/imp/journal-wip/<id>."""
    from imp.engine import worktree
    art = root / ".imp" / "change" / "artifacts"
    dest = art / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body)
    files = {str(p.relative_to(art)): p for p in sorted(art.rglob("*")) if p.is_file()}
    worktree.artifact_commit(root, change_id, files, message="artifact: test")


def _run_id_capture(script: str, root: Path, change_id: str):
    return subprocess.run(
        ["bash", str(SCRIPTS / script)], cwd=SCRIPTS,
        env={"IMP_PROJECT_ROOT": str(root), "IMP_CHANGE_ID": change_id,
             "IMP_CHANGE_DIR": str(root / ".imp" / "change"),
             "PATH": __import__("os").environ["PATH"]},
        capture_output=True, text=True,
    )


def _run_id(script: str, root: Path, change_id: str) -> int:
    return _run_id_capture(script, root, change_id).returncode


def test_spec_verify_passes_when_wip_matches_live(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".imp").mkdir()                       # gitignored stage for the temp index
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    _wip_commit(root, "id1", "spec/id1.md", "# spec\n")
    assert _run_id("spec-commit-verify.sh", root, "id1") == 0


def test_spec_verify_fails_when_wip_ref_absent(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".imp" / "change" / "artifacts" / "spec").mkdir(parents=True)
    (root / ".imp" / "change" / "artifacts" / "spec" / "id1.md").write_text("# spec\n")
    # No artifact_commit -> no wip ref.
    assert _run_id("spec-commit-verify.sh", root, "id1") != 0


def test_spec_verify_fails_when_blob_absent_from_wip(tmp_path: Path) -> None:
    # Wip ref exists (from a different artifact kind) but carries no spec blob.
    root = _repo(tmp_path)
    (root / ".imp").mkdir()
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    # Commit only a plan artifact — spec/<id>.md is absent from the wip tree.
    _wip_commit(root, "id1", "plans/id1-plan-1.md", "# plan\n")
    # Live spec file exists but was never committed to the wip tree.
    (root / ".imp" / "change" / "artifacts" / "spec").mkdir(parents=True)
    (root / ".imp" / "change" / "artifacts" / "spec" / "id1.md").write_text("# spec\n")
    assert _run_id("spec-commit-verify.sh", root, "id1") != 0


def test_spec_verify_fails_when_live_differs_from_committed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".imp").mkdir()
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    _wip_commit(root, "id1", "spec/id1.md", "# spec\n")
    # The agent edited the live file after committing it -> blob mismatch.
    (root / ".imp" / "change" / "artifacts" / "spec" / "id1.md").write_text("# edited\n")
    assert _run_id("spec-commit-verify.sh", root, "id1") != 0


def test_plan_verify_passes_on_newest_committed_plan(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".imp").mkdir()
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    _wip_commit(root, "id1", "plans/id1-plan-1.md", "# plan 1\n")
    _wip_commit(root, "id1", "plans/id1-plan-2.md", "# plan 2\n")
    assert _run_id("plan-commit-verify.sh", root, "id1") == 0


def test_plan_verify_fails_when_newest_plan_uncommitted(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".imp").mkdir()
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    _wip_commit(root, "id1", "plans/id1-plan-1.md", "# plan 1\n")
    # A newer plan file exists live but was never committed to the wip ref.
    (root / ".imp" / "change" / "artifacts" / "plans"
     / "id1-plan-2.md").write_text("# plan 2\n")
    assert _run_id("plan-commit-verify.sh", root, "id1") != 0


def test_plan_verify_passes_after_plan_revision(tmp_path: Path) -> None:
    # A revision is just another wip commit (no fixup machinery): re-snapshot the
    # edited plan and the gate passes off the latest blob.
    root = _repo(tmp_path)
    (root / ".imp").mkdir()
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    _wip_commit(root, "id1", "plans/id1-plan-1.md", "# plan 1\n")
    _wip_commit(root, "id1", "plans/id1-plan-1.md", "# plan 1 (revised)\n")
    assert _run_id("plan-commit-verify.sh", root, "id1") == 0


def test_plan_verify_fails_when_live_plan_differs_from_committed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".imp").mkdir()
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    _wip_commit(root, "id1", "plans/id1-plan-1.md", "# plan 1\n")
    # Agent edited the live plan after committing it -> blob mismatch.
    (root / ".imp" / "change" / "artifacts" / "plans"
     / "id1-plan-1.md").write_text("# plan 1 edited\n")
    assert _run_id("plan-commit-verify.sh", root, "id1") != 0


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
    _wip_commit(root, "id1", "plans/id1-plan-1.md", body)
    proc = _run_id_capture("plan-commit-verify.sh", root, "id1")
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
    _wip_commit(root, "id1", "plans/id1-plan-1.md", body)
    proc = _run_id_capture("plan-commit-verify.sh", root, "id1")
    assert proc.returncode == 0


def test_plan_verify_passes_plan_at_task_ceiling(tmp_path: Path) -> None:
    # Exactly at the ceiling (12 tasks): the guard uses > so 12 must pass.
    root = _repo(tmp_path)
    (root / ".imp").mkdir()
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    body = "# plan 1\n\n" + "".join(
        f"## Task {i}: t{i}\n\nbody\n\n" for i in range(1, 13))    # 12 tasks
    _wip_commit(root, "id1", "plans/id1-plan-1.md", body)
    proc = _run_id_capture("plan-commit-verify.sh", root, "id1")
    assert proc.returncode == 0


def test_spec_verify_reads_live_from_change_dir(tmp_path: Path) -> None:
    import os
    import shutil
    root = _repo(tmp_path)
    (root / ".imp").mkdir()
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    _wip_commit(root, "id1", "spec/id1.md", "# spec\n")
    # change-home is a separate dir; remove the in-project copy so a script that
    # still read $IMP_PROJECT_ROOT/.imp/change would find nothing.
    shutil.rmtree(root / ".imp" / "change" / "artifacts")
    home = tmp_path / "home"
    (home / "artifacts" / "spec").mkdir(parents=True)
    (home / "artifacts" / "spec" / "id1.md").write_text("# spec\n")
    proc = subprocess.run(
        ["bash", str(SCRIPTS / "spec-commit-verify.sh")], cwd=SCRIPTS,
        env={"IMP_PROJECT_ROOT": str(root),
             "IMP_CHANGE_ID": "id1", "IMP_CHANGE_DIR": str(home),
             "PATH": os.environ["PATH"]},
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_plan_verify_reads_live_from_change_dir(tmp_path: Path) -> None:
    import os
    import shutil
    root = _repo(tmp_path)
    (root / ".imp").mkdir()
    (root / ".gitignore").write_text(".imp/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    _wip_commit(root, "id1", "plans/id1-plan-1.md", "# plan 1\n")
    # Live plan exists ONLY at a separate change-home, not under the project tree.
    shutil.rmtree(root / ".imp" / "change" / "artifacts")
    home = tmp_path / "home"
    (home / "artifacts" / "plans").mkdir(parents=True)
    (home / "artifacts" / "plans" / "id1-plan-1.md").write_text("# plan 1\n")
    proc = subprocess.run(
        ["bash", str(SCRIPTS / "plan-commit-verify.sh")], cwd=SCRIPTS,
        env={"IMP_PROJECT_ROOT": str(root),
             "IMP_CHANGE_ID": "id1", "IMP_CHANGE_DIR": str(home),
             "PATH": os.environ["PATH"]},
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
