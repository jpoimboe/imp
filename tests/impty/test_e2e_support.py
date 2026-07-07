from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.impty.e2e_support import seed_minimal_repo, write_imp_shim


def _scripted_env(script_path: Path) -> dict[str, str]:
    return {**os.environ, "IMP_SCRIPTED_SCRIPT": str(script_path), "IMP_THEME": "ansi"}


def test_seed_minimal_repo_runs_real_imp_new_to_a_scripted_side_effect(
    tmp_path: Path,
) -> None:
    sentinel = "IMP_DOGFOOD_supp01"
    script = {"default": [{"text": "working", "tools": [
        {"name": "create_and_enter_worktree", "input": {}},
        {"name": "bash", "input": {"command": (
            'cd "$IMP_PROJECT_ROOT" && mkdir -p src && '
            f'printf "# {sentinel}\\n" > src/marker.py && '
            'git add src/marker.py && git commit -qm "feat: marker"')}},
        {"name": "complete_step", "input": {"status": "Success", "outcome": "ok"}},
    ]}]}
    script_path = seed_minimal_repo(tmp_path, script=script)

    proc = subprocess.run(
        [sys.executable, "-m", "imp", "new", "demo", "--non-interactive"],
        cwd=tmp_path, env=_scripted_env(script_path),
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr

    # The scripted bash ran as a REAL side effect on the change branch in the worktree.
    markers = list(tmp_path.glob(".imp/worktrees/*/src/marker.py"))
    assert markers, f"no committed scripted marker; stderr:\n{proc.stderr}"
    assert sentinel in markers[0].read_text()


def test_write_imp_shim_is_executable_and_runs_imp(tmp_path: Path) -> None:
    shim = write_imp_shim(tmp_path)
    assert os.access(shim, os.X_OK)
    out = subprocess.run([shim, "--help"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0
    assert "usage: imp" in (out.stdout + out.stderr)


def test_default_workflow_script_covers_the_fast_path_agents() -> None:
    from tests.impty.e2e_support import default_workflow_script

    script = default_workflow_script("IMP_DOGFOOD_t2t2t2")
    steps = script["steps"]
    for agent in ("threshold", "spec-finalizer", "plan-writer", "task-lead",
                  "plan-code-reviewer", "deferred-sweep", "gap-check", "finalize"):
        assert agent in steps, f"{agent} missing from the default-workflow script"

    # threshold goes live and chooses the express ceremony → fast path.
    threshold_done = steps["threshold"][0]["tools"][-1]["input"]
    assert threshold_done["ceremony"] == "express"
    lead_bash = steps["task-lead"][0]["tools"][0]["input"]["command"]
    assert "IMP_DOGFOOD_t2t2t2" in lead_bash and "src/sentinel.py" in lead_bash
    lead_done = steps["task-lead"][0]["tools"][-1]["input"]
    assert lead_done["done"] is True
    assert steps["finalize"][0]["tools"][-1]["input"]["decision"] == "merge"


def test_seed_default_repo_installs_the_real_default_workflow(tmp_path: Path) -> None:
    from tests.impty.e2e_support import default_workflow_script, seed_default_repo

    script_path = seed_default_repo(
        tmp_path, script=default_workflow_script("IMP_DOGFOOD_seed01"))
    assert script_path.is_file()
    # The shipped default workflow (with its gate scripts) was copied in.
    wf = tmp_path / "imp" / "workflows" / "imp"
    assert (wf / "workflow.yaml").is_file()
    assert (wf / "scripts" / "cleanup.sh").is_file()
    assert (wf / "scripts" / "run-verify.sh").is_file()
    assert (wf / "agents" / "spec-finalizer.md").is_file()
