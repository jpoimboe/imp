"""The repo's build environment: the committed interpreter pin and the shape of
the Makefile's bootstrap targets."""

from __future__ import annotations

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
