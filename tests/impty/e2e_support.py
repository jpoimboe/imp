from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def can_pty() -> bool:
    try:
        import pty
        m, s = pty.openpty()
        os.close(m)
        os.close(s)
        return True
    except Exception:
        return False


def clean_env(**overrides: str) -> dict[str, str]:
    """Inherited env with outer ``IMP_*`` stripped (so an imp-on-imp run can't leak
    its engine vars into the spawned child), plus the given overrides."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("IMP_")}
    env.update(overrides)
    return env


def write_imp_shim(directory: Path) -> str:
    """Write an executable `imp` shim that execs the working-tree CLI."""
    shim = directory / "imp"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m imp "$@"\n')
    shim.chmod(0o755)
    return str(shim)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args],
                   check=True, capture_output=True, text=True)


_MINIMAL_CONFIG = (
    "providers: [scripted]\n"
    "tiers:\n"
    "  low: {scripted: x}\n"
    "  medium: {scripted: x}\n"
    "  high: {scripted: x}\n"
    "sandbox:\n  enabled: false\n"
)


def seed_minimal_repo(root: Path, *, script: dict[str, Any]) -> Path:
    """Seed a throwaway git repo wired to the scripted provider and return the script path."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n")
    (root / "README.md").write_text("seed\n")
    agents = root / "imp" / "workflows" / "imp" / "agents"
    agents.mkdir(parents=True)
    (agents / "coder.md").write_text(
        "---\nname: coder\ntools: [bash, complete_step, create_and_enter_worktree]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\nprompt body\n")
    (root / "imp" / "workflows" / "imp" / "workflow.yaml").write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n  - name: work\n    steps:\n"
        "      - name: coder\n        file: agents/coder.md\n")
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(_MINIMAL_CONFIG)
    script_path = root / "script.json"
    script_path.write_text(json.dumps(script))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup")
    return script_path


def default_workflow_script(sentinel: str) -> dict[str, Any]:
    """Canned script driving the real default workflow to completion via the trivial fast path."""
    def bash(cmd: str) -> dict[str, Any]:
        return {"name": "bash", "input": {"command": cmd}}

    def done(**fields: Any) -> dict[str, Any]:
        return {"name": "complete_step", "input": {"status": "Success", "outcome": "ok", **fields}}

    spec_cmd = (
        'mkdir -p "$IMP_CHANGE_DIR/artifacts/spec" && '
        'printf "# Spec\\n\\nstub spec\\n"'
        ' > "$IMP_CHANGE_DIR/artifacts/spec/$IMP_CHANGE_ID.md" && '
        'imp artifact-commit spec'
    )
    plan_cmd = (
        'mkdir -p "$IMP_CHANGE_DIR/artifacts/plans" && '
        'printf "# Plan 1\\n" > "$IMP_CHANGE_DIR/artifacts/plans/$IMP_CHANGE_ID-plan-1.md" && '
        'imp artifact-commit plan'
    )
    lead_cmd = (
        'cd "$IMP_PROJECT_ROOT" && mkdir -p src && '
        f'printf "# {sentinel}\\n" > src/sentinel.py && '
        'git add src/sentinel.py && git commit -qm "feat: add sentinel"'
    )
    return {
        "steps": {
            "front-door": [{"text": "front door", "tools": [
                {"name": "rename_change", "input": {"name": "demo change"}},
                {"name": "create_and_enter_worktree", "input": {}},
                bash(spec_cmd),
                done(ceremony="express"),
            ]}],
            "spec-writer": [{"text": "spec", "tools": [
                done(phase="written",
                     spec_path=".imp/change/artifacts/spec/SCRIPTED-STUB.md",
                     complexity="trivial"),
            ]}],
            "spec-reviewer": [{"text": "review", "tools": [
                done(findings="No findings.", blocking=False, has_minor_findings=False),
            ]}],
            "plan-writer": [{"text": "plan", "tools": [
                bash(plan_cmd),
                done(plan_path=".imp/change/artifacts/plans/SCRIPTED-STUB-plan-1.md",
                     plan_number=1),
            ]}],
            "plan-finalized": [{"text": "finalize plan", "tools": [
                done(task_total=1),
            ]}],
            "task-lead": [{"text": "implement", "tools": [
                bash(lead_cmd),
                done(done=True, deviation=False, verify_tolerated=False, summary="done"),
            ]}],
            "code-reviewer": [{"text": "review", "tools": [
                done(verdict="approve", findings=""),
            ]}],
            "deferred-sweep": [{"text": "sweep", "tools": [
                done(summary="no deferred items"),
            ]}],
            "plan-cycle-check": [{"text": "check", "tools": [
                done(more_plans=False, plan_total=1,
                     rationale="single plan covers the spec"),
            ]}],
            "finalize": [{"text": "finalize", "tools": [
                done(decision="merge", comments="", built="sentinel",
                     verify_status="n/a", open_items="none", next_steps="none"),
            ]}],
        },
        "default": [{"text": "ok", "tools": [done()]}],
    }


def seed_default_repo(root: Path, *, script: dict[str, Any]) -> Path:
    """Like seed_minimal_repo, but installs the REAL shipped default workflow."""
    from imp.assets import bundled_workflow_dir

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n")
    (root / "README.md").write_text("seed\n")
    (root / "imp" / "workflows").mkdir(parents=True)
    shutil.copytree(bundled_workflow_dir("imp"),
                    root / "imp" / "workflows" / "imp")
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(_MINIMAL_CONFIG)
    script_path = root / "script.json"
    script_path.write_text(json.dumps(script))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup")
    return script_path
