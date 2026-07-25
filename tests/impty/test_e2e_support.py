from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.impty.e2e_support import (
    E2E_SUBPROCESS_TIMEOUT,
    clean_env,
    seed_minimal_repo,
    write_imp_shim,
)


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
        [sys.executable, "-m", "imp", "new", "demo", "--title", "demo change", "--headless"],
        cwd=tmp_path,
        env=clean_env(IMP_SCRIPTED_SCRIPT=str(script_path), IMP_THEME="ansi"),
        capture_output=True, text=True, timeout=E2E_SUBPROCESS_TIMEOUT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # The scripted bash committed a REAL side effect on the quest branch imp/<id>.
    # The no-merge completion removes the worktree, so read the marker from the branch.
    branches = subprocess.run(
        ["git", "-C", str(tmp_path), "branch", "--format=%(refname:short)",
         "--list", "imp/*"],
        capture_output=True, text=True).stdout.split()
    assert branches, f"no imp/<id> branch; stderr:\n{proc.stderr}"
    marker = subprocess.run(
        ["git", "-C", str(tmp_path), "show", f"{branches[0]}:src/marker.py"],
        capture_output=True, text=True)
    assert marker.returncode == 0, marker.stderr
    assert sentinel in marker.stdout


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
                  "batch-code-reviewer", "deferred-sweep", "gap-check", "finalize"):
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
    wf = tmp_path / "imp" / "workflows" / "default"
    assert (wf / "workflow.yaml").is_file()
    assert (wf / "scripts" / "cleanup.sh").is_file()
    assert (wf / "scripts" / "run-verify.sh").is_file()
    assert (wf / "agents" / "spec-finalizer.md").is_file()


def test_clean_env_bakes_the_connect_budget() -> None:
    env = clean_env()
    assert env["IMP_SESSION_CONNECT_TIMEOUT"] == "240"


def test_clean_env_explicit_connect_override_wins() -> None:
    env = clean_env(IMP_SESSION_CONNECT_TIMEOUT="5")
    assert env["IMP_SESSION_CONNECT_TIMEOUT"] == "5"


def test_e2e_subprocess_timeout_is_below_the_pytest_ceiling() -> None:
    # 235 < the 240s pytest-timeout ceiling, so the child is killed with captured output.
    assert E2E_SUBPROCESS_TIMEOUT == 235


def test_clean_env_keeps_imp_home_and_strips_other_imp_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # IMP_HOME locates the per-test global config layer. Strip it with the other
    # IMP_* engine vars and an impty child reads the developer's real
    # ~/.imp/config.yaml, whose `providers` outrank the seeded project layer.
    monkeypatch.setenv("IMP_HOME", "/tmp/imp-home-x")
    monkeypatch.setenv("IMP_QUEST_ID", "leaked")
    env = clean_env()
    assert env["IMP_HOME"] == "/tmp/imp-home-x"
    assert "IMP_QUEST_ID" not in env
