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
