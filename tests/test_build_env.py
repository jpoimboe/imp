"""The repo's build environment: the committed interpreter pin and the shape of
the Makefile's bootstrap targets."""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_python_version_pins_the_exact_patch() -> None:
    # A minor-only pin ("3.14") resolves to the SYSTEM /usr/bin/python3.14 on a
    # box where its patch outranks the uv-managed build; only the exact patch
    # selects uv's own interpreter. And the pin only reaches other clones and
    # worktrees if git actually tracks it.
    assert (_REPO_ROOT / ".python-version").read_text() == "3.14.4\n"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".python-version"],
        cwd=_REPO_ROOT, capture_output=True, text=True,
    )
    assert tracked.returncode == 0, "git does not track .python-version"


def test_pyproject_declares_no_tool_uv_section() -> None:
    # python-preference = "only-managed" invalidates and recreates .venv on the
    # first run (uv >= 0.8.0) and pins no patch version at all; the exact-patch
    # .python-version is the whole mechanism.
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    assert "uv" not in data.get("tool", {})


def _make_n(target: str) -> subprocess.CompletedProcess[str]:
    """Expand a target's recipe without running it, so the assertions pin the
    real expanded command rather than Makefile source text."""
    # LC_ALL=C: make's diagnostics are gettext-translated, and one assertion
    # below reads make's own error text.
    return subprocess.run(
        ["make", "-n", target], cwd=_REPO_ROOT, capture_output=True, text=True,
        env={**os.environ, "LC_ALL": "C"},
    )


def test_sync_env_runs_uv_sync_with_all_extras() -> None:
    # `dev` is an extra, not a dependency-group, so --all-extras is what
    # installs the test tooling into this worktree's .venv.
    out = _make_n("sync-env")
    assert out.returncode == 0, out.stderr
    assert "uv sync --all-extras" in out.stdout
    # The export|pip-install form installed into whatever interpreter happened
    # to be active, which is what left the gate running on distro packages.
    assert "pip install" not in out.stdout


def test_install_target_is_gone() -> None:
    # A second bootstrap path re-resolves pyproject's loose ranges and installs
    # whatever is newest — exactly the drift `make depcheck` exists to catch.
    out = _make_n("install")
    assert out.returncode != 0
    assert "No rule to make target 'install'" in out.stderr


def test_pip_scope_is_gone() -> None:
    # PIP_SCOPE existed only to serve the deleted pip form.
    assert "PIP_SCOPE" not in (_REPO_ROOT / "Makefile").read_text()
