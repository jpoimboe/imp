"""Tests for the shell scripts shipped in the default workflow.

These run the actual scripts as subprocesses with controlled env vars and
working directories — catches regressions in the dogfood path that pure
unit tests miss.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from imp.assets import default_workflow_dir


def _scripts_dir() -> Path:
    return default_workflow_dir() / "scripts"


def _run(script: str, *args: str, env_extra: dict[str, str] | None = None,
         cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [str(_scripts_dir() / script), *args],
        env=env, cwd=cwd, capture_output=True, text=True, check=False,
    )


# ── run-tests.sh: IMP_VERIFY_COMMAND contract ───────────────────────────────────
# Note: check-artifact.sh was removed in the superpowers workflow port.
# Artifact validation is now handled by spec-commit-verify.sh and
# plan-commit-verify.sh; see test_commit_verify_scripts.py for their tests.
# The test_check_artifact_script.py file verifies those replacements exist.


def test_run_tests_command_succeeds(tmp_path: Path) -> None:
    """IMP_VERIFY_COMMAND set to a command that succeeds → script exits 0."""
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run(
        "run-tests.sh",
        env_extra={
            "IMP_PROJECT_ROOT": str(project),
            "IMP_VERIFY_COMMAND": "true",
        },
    )
    assert proc.returncode == 0, (
        f"expected exit 0 for succeeding IMP_VERIFY_COMMAND; got {proc.returncode}. "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


def test_run_tests_command_fails(tmp_path: Path) -> None:
    """IMP_VERIFY_COMMAND set to a command that fails → script exits non-zero."""
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run(
        "run-tests.sh",
        env_extra={
            "IMP_PROJECT_ROOT": str(project),
            "IMP_VERIFY_COMMAND": "false",
        },
    )
    assert proc.returncode != 0, (
        f"expected non-zero exit for failing IMP_VERIFY_COMMAND; got {proc.returncode}. "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


def test_run_tests_timeout_exits_124_with_diagnosis(tmp_path: Path) -> None:
    """A verify command exceeding IMP_VERIFY_TIMEOUT → exit 124 plus an explicit
    timed-out diagnosis on stderr. Without the diagnosis the gate feedback is
    just output truncated mid-run, which reads as a hang at the last printed
    step and sends the resumed task-lead hunting a phantom hanging command."""
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run(
        "run-tests.sh",
        env_extra={
            "IMP_PROJECT_ROOT": str(project),
            "IMP_VERIFY_COMMAND": "sleep 5",
            "IMP_VERIFY_TIMEOUT": "1",
        },
    )
    assert proc.returncode == 124, (
        f"expected exit 124 (timeout) ; got {proc.returncode}. "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "timed out after 1s" in proc.stderr, (
        f"expected timeout diagnosis on stderr; got {proc.stderr!r}"
    )


def test_run_tests_no_command_passes(tmp_path: Path) -> None:
    """IMP_VERIFY_COMMAND unset → script exits 0 (no-op) with informational message."""
    project = tmp_path / "proj"
    project.mkdir()
    # Ensure IMP_VERIFY_COMMAND is not inherited from the outer environment.
    env_extra: dict[str, str] = {"IMP_PROJECT_ROOT": str(project)}
    env = os.environ.copy()
    env.pop("IMP_VERIFY_COMMAND", None)
    env.update(env_extra)
    proc = subprocess.run(
        [str(_scripts_dir() / "run-tests.sh")],
        env=env, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, (
        f"expected exit 0 when IMP_VERIFY_COMMAND unset; got {proc.returncode}. "
        f"stderr={proc.stderr!r}"
    )
    assert "no verify_command configured" in proc.stdout, (
        f"expected 'no verify_command configured' in stdout; got {proc.stdout!r}"
    )


def test_run_tests_empty_command_passes(tmp_path: Path) -> None:
    """IMP_VERIFY_COMMAND set to empty string → script exits 0 (no-op)."""
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run(
        "run-tests.sh",
        env_extra={
            "IMP_PROJECT_ROOT": str(project),
            "IMP_VERIFY_COMMAND": "",
        },
    )
    assert proc.returncode == 0, (
        f"expected exit 0 when IMP_VERIFY_COMMAND is empty; got {proc.returncode}. "
        f"stderr={proc.stderr!r}"
    )
    assert "no verify_command configured" in proc.stdout, (
        f"expected 'no verify_command configured' in stdout; got {proc.stdout!r}"
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
