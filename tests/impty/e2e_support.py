from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from imp.engine.state import LockHeld, ProjectLock

# Outer subprocess cap, just under the 240s pytest-timeout ceiling: connect 240 >= 235 < 240.
E2E_SUBPROCESS_TIMEOUT = 235
# Shared inner-poll ceiling; a loaded host lifts every adopting test via IMP_E2E_POLL_TIMEOUT.
E2E_POLL_TIMEOUT = float(os.environ.get("IMP_E2E_POLL_TIMEOUT", "60"))
# The pty repaint of the interrupt-steer hint trails the durable gate event by an
# extra cross-process relay -> prompt_toolkit render (3 async hops over a socket),
# which -n auto CPU saturation can stretch past the base poll ceiling. Give that one
# render-bound poll a generous multiple so it never false-fires on scheduling lag;
# the 240s per-test outer timeout stays the single backstop for a genuine no-paint.
E2E_RENDER_POLL_TIMEOUT = E2E_POLL_TIMEOUT * 2
# Flushing quest state and reaching exit after a SIGINT can exceed the base poll
# under -n auto CPU/disk starvation; give that one process-exit wait a generous
# multiple so it never false-fires before the 240s outer catches a genuine hang.
# Distinct from E2E_RENDER_POLL_TIMEOUT (same value; pty repaint vs. process exit).
E2E_EXIT_POLL_TIMEOUT = E2E_POLL_TIMEOUT * 2


def _poll(predicate, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def _flock_free(lock_path: Path) -> bool:
    try:
        with ProjectLock(lock_path).acquire(timeout=0):
            return True
    except LockHeld:
        return False


def _event_types(events_path: Path) -> list[str]:
    if not events_path.exists():
        return []
    out: list[str] = []
    for line in events_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line)["type"])
        except (json.JSONDecodeError, KeyError):
            continue
    return out


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
    its engine vars into the spawned child), plus the given overrides. ``IMP_HOME``
    is the exception: it locates the per-test global config layer, so stripping it
    would make the child read the developer's real ~/.imp/config.yaml."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("IMP_") or k == "IMP_HOME"}
    # Baked before overrides so an explicit caller override still wins.
    env["IMP_SESSION_CONNECT_TIMEOUT"] = "240"
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
    agents = root / "imp" / "workflows" / "default" / "agents"
    agents.mkdir(parents=True)
    (agents / "coder.md").write_text(
        "---\nname: coder\ntools: [bash, complete_step, create_and_enter_worktree]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\nprompt body\n")
    (root / "imp" / "workflows" / "default" / "workflow.yaml").write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n  - name: work\n    steps:\n"
        "      - name: coder\n        file: agents/coder.md\n")
    (root / "imp" / "project.yaml").write_text(
        "default-workflow: default\ndefault-base: main\n")
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
        'mkdir -p "$IMP_QUEST_DIR/artifacts/spec" && '
        'printf "# Spec\\n\\nstub spec\\n"'
        ' > "$IMP_QUEST_DIR/artifacts/spec/$IMP_QUEST_ID.md"'
    )
    plan_cmd = (
        'mkdir -p "$IMP_QUEST_DIR/artifacts/plans" && '
        'printf "# Plan 1\\n" > "$IMP_QUEST_DIR/artifacts/plans/$IMP_QUEST_ID-plan-1.md"'
    )
    lead_cmd = (
        'cd "$IMP_PROJECT_ROOT" && mkdir -p src && '
        f'printf "# {sentinel}\\n" > src/sentinel.py && '
        'git add src/sentinel.py && git commit -qm "feat: add sentinel"'
    )
    return {
        "steps": {
            "threshold": [{"text": "threshold", "tools": [
                {"name": "rename_quest", "input": {"name": "demo change"}},
                {"name": "create_and_enter_worktree", "input": {}},
                bash(spec_cmd),
                done(ceremony="express"),
            ]}],
            "spec-finalizer": [{"text": "spec", "tools": [
                done(phase="written",
                     spec_path=".imp/quests/test-quest/artifacts/spec/SCRIPTED-STUB.md",
                     complexity="trivial"),
            ]}],
            "spec-reviewer": [{"text": "review", "tools": [
                done(findings="No findings.", blocking=False, has_minor_findings=False),
            ]}],
            "plan-writer": [{"text": "plan", "tools": [
                bash(plan_cmd),
                done(plan_path=".imp/quests/test-quest/artifacts/plans/SCRIPTED-STUB-plan-1.md",
                     plan_number=1, plan_total=1),
            ]}],
            "task-lead": [{"text": "implement", "tools": [
                bash(lead_cmd),
                done(done=True, verify_tolerated=False, summary="done"),
            ]}],
            "batch-code-reviewer": [{"text": "review", "tools": [
                done(verdict="approve", findings=""),
            ]}],
            "deferred-sweep": [{"text": "sweep", "tools": [
                done(converge_verify_tolerated=False,
                     summary="no deferred items"),
            ]}],
            "gap-check": [{"text": "check", "tools": [
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
    shutil.copytree(bundled_workflow_dir("default"),
                    root / "imp" / "workflows" / "default")
    (root / "imp" / "project.yaml").write_text(
        "default-workflow: default\ndefault-base: main\n")
    (root / ".imp").mkdir(exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(_MINIMAL_CONFIG)
    script_path = root / "script.json"
    script_path.write_text(json.dumps(script))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup")
    return script_path
