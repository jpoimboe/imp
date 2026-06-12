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
    agents = root / "imp" / "workflows" / "default" / "agents"
    agents.mkdir(parents=True)
    (agents / "coder.md").write_text(
        "---\nname: coder\ntools: [bash, complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\nprompt body\n")
    (root / "imp" / "workflows" / "default" / "workflow.yaml").write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n  - name: work\n    steps:\n"
        "      - name: coder\n        file: agents/coder.md\n"
        "        fs_permissions:\n"
        "          - {operations: [read],  paths: [\"**\"], mode: allow}\n"
        "          - {operations: [write], paths: [\"**\"], mode: allow}\n")
    (root / "imp" / "project.yaml").write_text("workflow: default\n")
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
        return {"name": "complete_step", "input": {"status": "Success", **fields}}

    spec_cmd = (
        'cd "$IMP_PROJECT_ROOT" && mkdir -p imp/specs && '
        'printf "# Spec\\n\\nstub spec\\n" > "imp/specs/$IMP_CHANGE_SLUG.md" && '
        'git add "imp/specs/$IMP_CHANGE_SLUG.md" && git commit -qm "spec: add spec"'
    )
    plan_cmd = (
        'cd "$IMP_PROJECT_ROOT" && mkdir -p imp/plans && '
        'printf "# Plan 1\\n" > "imp/plans/$IMP_CHANGE_SLUG-plan-1.md" && '
        'git add "imp/plans/$IMP_CHANGE_SLUG-plan-1.md" && '
        'git commit -qm "docs: add plan 1"'
    )
    lead_cmd = (
        'cd "$IMP_PROJECT_ROOT" && mkdir -p src && '
        f'printf "# {sentinel}\\n" > src/sentinel.py && '
        'git add src/sentinel.py && git commit -qm "feat: add sentinel"'
    )
    return {
        "steps": {
            "spec-writer": [{"text": "spec", "tools": [
                bash(spec_cmd),
                done(phase="written", spec_path="imp/specs/SCRIPTED-STUB.md",
                     complexity="trivial", unseen_changes=False),
            ]}],
            "plan-writer": [{"text": "plan", "tools": [
                bash(plan_cmd),
                done(plan_path="imp/plans/SCRIPTED-STUB-plan-1.md", plan_number=1),
            ]}],
            "task-lead": [{"text": "implement", "tools": [
                bash(lead_cmd),
                done(done=True, deviation=False, summary="done"),
            ]}],
            "code-reviewer": [{"text": "review", "tools": [
                done(verdict="approve", findings=""),
            ]}],
            "summarizer": [{"text": "summary", "tools": [
                done(built="sentinel", test_status="n/a",
                     open_items="none", next_steps="none"),
            ]}],
            "final-review": [{"text": "final", "tools": [
                done(decision="merge", comments=""),
            ]}],
        },
        "default": [{"text": "ok", "tools": [done()]}],
    }


def seed_default_repo(root: Path, *, script: dict[str, Any]) -> Path:
    """Like seed_minimal_repo, but installs the REAL shipped default workflow."""
    from imp.assets import default_workflow_dir

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n")
    (root / "README.md").write_text("seed\n")
    (root / "imp" / "workflows").mkdir(parents=True)
    shutil.copytree(default_workflow_dir(),
                    root / "imp" / "workflows" / "default")
    (root / "imp" / "project.yaml").write_text("workflow: default\n")
    (root / ".imp").mkdir(exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(_MINIMAL_CONFIG)
    script_path = root / "script.json"
    script_path.write_text(json.dumps(script))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup")
    return script_path
