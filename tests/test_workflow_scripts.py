"""Tests for the shell scripts shipped in the default workflow.

These run the actual scripts as subprocesses with controlled env vars and
working directories — catches regressions in the dogfood path that pure
unit tests miss.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow


def _scripts_dir() -> Path:
    return bundled_workflow_dir("imp") / "scripts"


def _run(script: str, *args: str, env_extra: dict[str, str] | None = None,
         cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [str(_scripts_dir() / script), *args],
        env=env, cwd=cwd, capture_output=True, text=True, check=False,
    )


# ── run-verify.sh: combined gate with tree-SHA skip cache ───────────────────


def _git_init(project: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=project, check=True)


def _git_commit_all(project: Path, msg: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=project,
                   env={**os.environ, "GIT_EDITOR": "true"}, check=True)


def test_run_verify_command_succeeds(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run("run-verify.sh", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_VERIFY_COMMAND": "true"})
    assert proc.returncode == 0, proc.stderr


def test_run_verify_command_fails(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run("run-verify.sh", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_VERIFY_COMMAND": "false"})
    assert proc.returncode != 0


def test_run_verify_timeout_exits_124_with_diagnosis(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run("run-verify.sh", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_VERIFY_COMMAND": "sleep 5",
        "IMP_VERIFY_TIMEOUT": "1"})
    assert proc.returncode == 124, proc.stderr
    assert "timed out after 1s" in proc.stderr


def test_run_verify_no_command_passes(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    env = os.environ.copy()
    env.pop("IMP_VERIFY_COMMAND", None)
    env["IMP_PROJECT_ROOT"] = str(project)
    proc = subprocess.run([str(_scripts_dir() / "run-verify.sh")],
                          env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    assert "no verify_command configured" in proc.stdout


def test_run_verify_empty_command_passes(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run("run-verify.sh", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_VERIFY_COMMAND": ""})
    assert proc.returncode == 0
    assert "no verify_command configured" in proc.stdout


def test_run_verify_skips_on_unchanged_tree(tmp_path: Path) -> None:
    # First green run records HEAD's tree; a second run at the SAME tree skips
    # (and does NOT re-run the command); a new commit re-runs it.
    project = tmp_path / "proj"
    project.mkdir()
    _git_init(project)
    (project / "a.txt").write_text("1")
    _git_commit_all(project, "c1")
    counter = project / "count"
    cmd = f'sh -c "echo x >> {counter}"'

    first = _run("run-verify.sh", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_VERIFY_COMMAND": cmd})
    assert first.returncode == 0
    assert counter.read_text().count("x") == 1

    second = _run("run-verify.sh", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_VERIFY_COMMAND": cmd})
    assert second.returncode == 0
    assert "verify skipped — tree unchanged since last green run" in second.stdout
    assert counter.read_text().count("x") == 1   # command NOT re-run

    (project / "a.txt").write_text("2")
    _git_commit_all(project, "c2")
    third = _run("run-verify.sh", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_VERIFY_COMMAND": cmd})
    assert third.returncode == 0
    assert counter.read_text().count("x") == 2   # tree changed => re-ran


def test_verify_and_sweep_verify_share_tree_skip_cache(tmp_path: Path) -> None:
    # Both verify and sweep-verify call the same script, so they share the tree-skip cache.
    wf = load_workflow(bundled_workflow_dir("imp"))
    by = {s.name: s for s in wf.flat_steps()}
    assert by["verify"].command == "./scripts/run-verify.sh"
    assert by["sweep-verify"].command == "./scripts/run-verify.sh"

    project = tmp_path / "proj"
    project.mkdir()
    _git_init(project)
    (project / "a.txt").write_text("1")
    _git_commit_all(project, "c1")
    counter = project / "count"
    cmd = f'sh -c "echo x >> {counter}"'

    # First invocation (the `verify` step): runs the command, records the green tree.
    verify = _run("run-verify.sh", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_VERIFY_COMMAND": cmd})
    assert verify.returncode == 0, verify.stderr
    assert counter.read_text().count("x") == 1

    # Second invocation (the `sweep-verify` step) at the SAME tree -> cache hit.
    sweep_verify = _run("run-verify.sh", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_VERIFY_COMMAND": cmd})
    assert sweep_verify.returncode == 0, sweep_verify.stderr
    assert "verify skipped — tree unchanged since last green run" in sweep_verify.stdout
    assert counter.read_text().count("x") == 1   # sweep-verify did NOT re-run


