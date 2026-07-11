"""The spec/plan commit-verify gates: each passes iff the agent produced its
artifact file on disk under the change's artifacts/ dir. The engine owns
journaling now, so the gates check on-disk presence, not git commit state."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from imp.assets import bundled_workflow_dir

SCRIPTS = bundled_workflow_dir("imp") / "scripts"


def _run_capture(script: str, root: Path, quest_dir: Path, quest_id: str):
    return subprocess.run(
        ["bash", str(SCRIPTS / script)], cwd=SCRIPTS,
        env={"IMP_PROJECT_ROOT": str(root), "IMP_QUEST_ID": quest_id,
             "IMP_QUEST_DIR": str(quest_dir), "PATH": os.environ["PATH"]},
        capture_output=True, text=True,
    )


def _run(script: str, root: Path, quest_dir: Path, quest_id: str) -> int:
    return _run_capture(script, root, quest_dir, quest_id).returncode


def _write(quest_dir: Path, rel: str, body: str) -> None:
    dest = quest_dir / "artifacts" / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body)


def test_spec_verify_passes_when_artifact_present(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write(home, "spec/id1.md", "# spec\n")
    assert _run("spec-commit-verify.sh", tmp_path, home, "id1") == 0


def test_spec_verify_fails_when_artifact_absent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "artifacts" / "spec").mkdir(parents=True)   # dir exists, file does not
    assert _run("spec-commit-verify.sh", tmp_path, home, "id1") != 0


def test_plan_verify_passes_on_newest_of_multiple(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write(home, "plans/id1-plan-1.md", "# plan 1\n")
    _write(home, "plans/id1-plan-2.md", "# plan 2\n")
    proc = _run_capture("plan-commit-verify.sh", tmp_path, home, "id1")
    assert proc.returncode == 0, proc.stderr
    # The gate reports the newest plan by name.
    assert "id1-plan-2.md" in proc.stdout


def test_plan_verify_ignores_non_numeric_suffix(tmp_path: Path) -> None:
    # A file whose -plan-<suffix> is not a number is skipped; the newest numeric
    # plan is selected instead.
    home = tmp_path / "home"
    _write(home, "plans/id1-plan-1.md", "# plan 1\n")
    _write(home, "plans/id1-plan-abc.md", "# not a plan\n")
    proc = _run_capture("plan-commit-verify.sh", tmp_path, home, "id1")
    assert proc.returncode == 0, proc.stderr
    assert "id1-plan-1.md" in proc.stdout
    assert "id1-plan-abc.md" not in proc.stdout


def test_plan_verify_fails_when_no_plan_present(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "artifacts" / "plans").mkdir(parents=True)   # dir exists, no plan files
    assert _run("plan-commit-verify.sh", tmp_path, home, "id1") != 0


def test_plan_verify_passes_large_plan_no_task_cap(tmp_path: Path) -> None:
    # No task-count ceiling: a 20-task plan passes, and the success line carries
    # no "(N tasks)" suffix. The script stays capless.
    home = tmp_path / "home"
    body = "# plan 1\n\n" + "".join(
        f"## Task {i}: t{i}\n\nbody\n\n" for i in range(1, 21))   # 20 tasks
    _write(home, "plans/id1-plan-1.md", body)
    proc = _run_capture("plan-commit-verify.sh", tmp_path, home, "id1")
    assert proc.returncode == 0, proc.stderr
    assert "tasks)" not in proc.stdout
    imp = (SCRIPTS / "plan-commit-verify.sh").read_text()
    assert "CEILING" not in imp


def test_spec_verify_reads_live_from_quest_dir(tmp_path: Path) -> None:
    # The gate reads the artifact from IMP_QUEST_DIR (the change-home), which can
    # be a separate dir from the project tree.
    root = tmp_path / "proj"
    root.mkdir()
    home = tmp_path / "home"
    _write(home, "spec/id1.md", "# spec\n")
    proc = _run_capture("spec-commit-verify.sh", root, home, "id1")
    assert proc.returncode == 0, proc.stderr


def test_plan_verify_reads_live_from_quest_dir(tmp_path: Path) -> None:
    # The gate reads the plan from IMP_QUEST_DIR, not the project tree.
    root = tmp_path / "proj"
    root.mkdir()
    home = tmp_path / "home"
    _write(home, "plans/id1-plan-1.md", "# plan 1\n")
    proc = _run_capture("plan-commit-verify.sh", root, home, "id1")
    assert proc.returncode == 0, proc.stderr
