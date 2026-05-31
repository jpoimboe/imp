"""Tests for the shell scripts shipped in the default workflow.

These run the actual scripts as subprocesses with controlled env vars and
working directories — catches regressions in the dogfood path that pure
unit tests miss.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

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


# ── check-artifact.sh (Bug N infrastructure) ─────────────────────────────────


def test_check_artifact_passes_when_file_exists_and_nonempty(tmp_path: Path) -> None:
    """The script exits 0 when the given path points to a non-empty file."""
    (tmp_path / "spec.md").write_text("# Spec\n\nReal content.\n")
    proc = _run("check-artifact.sh", str(tmp_path / "spec.md"))
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "ok" in proc.stdout


def test_check_artifact_fails_when_file_missing(tmp_path: Path) -> None:
    """The script exits non-zero when the artifact doesn't exist."""
    proc = _run("check-artifact.sh", str(tmp_path / "nope.md"))
    assert proc.returncode != 0
    assert "missing" in proc.stderr


def test_check_artifact_fails_when_file_empty(tmp_path: Path) -> None:
    """The script exits non-zero when the artifact is empty (zero bytes)."""
    (tmp_path / "plan.md").write_text("")
    proc = _run("check-artifact.sh", str(tmp_path / "plan.md"))
    assert proc.returncode != 0
    assert "empty" in proc.stderr


def test_check_artifact_fails_when_no_arg() -> None:
    """The script exits non-zero when called with no path argument
    (the ${1:?...} guard fires)."""
    proc = subprocess.run(
        [str(_scripts_dir() / "check-artifact.sh")],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode != 0


# ── run-tests.sh robustness (Bug Q) ──────────────────────────────────────────


def test_run_tests_uses_python_m_pytest_not_bare_pytest(tmp_path: Path) -> None:
    """Bug Q: bare `pytest` may not be on subprocess PATH; `python3 -m pytest`
    is more reliable. Verify the script invokes the python module form."""
    script_text = (_scripts_dir() / "run-tests.sh").read_text()
    assert "python3 -m pytest" in script_text or "$PY -m pytest" in script_text
    # The script should NOT have a bare `pytest -q` invocation outside of
    # a python -m context (which would be the original Bug Q pattern).
    for line in script_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        # `pytest` may appear inside `python -m pytest` or `"$PY" -m pytest`
        # but not bare. Detect bare exec/run of pytest as the first word.
        if stripped.startswith(("pytest ", "exec pytest", "timeout 300 pytest ")):
            pytest.fail(
                f"run-tests.sh has a bare `pytest` invocation: {line!r} "
                "— this re-introduces Bug Q (pytest may not be on PATH "
                "in subprocess envs)"
            )


def test_run_tests_fails_loud_when_tests_dir_but_no_pytest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug Q corollary: tests/ exists but pytest unimportable → exit 2,
    not silent pass. Forced by pointing IMP_PYTHON at a python that
    doesn't have pytest installed."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "tests").mkdir()
    (project / "tests" / "test_x.py").write_text("def test_x(): assert True\n")

    # Pick a python interpreter that definitely won't have pytest:
    # write a tiny shim that always returns ImportError on `import pytest`.
    fake_python = tmp_path / "fake-python.sh"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        '# Reject any `import pytest` attempt.\n'
        'if [[ "$*" == *"import pytest"* ]]; then exit 1; fi\n'
        'exec /usr/bin/env python3 "$@"\n'
    )
    fake_python.chmod(0o755)

    proc = _run(
        "run-tests.sh",
        env_extra={
            "IMP_PROJECT_ROOT": str(project),
            "IMP_PYTHON": str(fake_python),
        },
    )
    assert proc.returncode == 2, (
        f"expected exit 2 (fail-loud); got {proc.returncode}. "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "not importable" in proc.stderr


def test_run_tests_passes_when_no_tests_dir_or_makefile(tmp_path: Path) -> None:
    """New project without tests/ or Makefile: silently pass (don't block
    a brand-new project's first Change)."""
    project = tmp_path / "empty-proj"
    project.mkdir()
    proc = _run(
        "run-tests.sh",
        env_extra={"IMP_PROJECT_ROOT": str(project)},
    )
    assert proc.returncode == 0, (
        f"expected exit 0 for empty project; got {proc.returncode}. "
        f"stderr={proc.stderr!r}"
    )
    assert "no tests found" in proc.stdout
