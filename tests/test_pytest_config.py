"""Locking test: pytest pythonpath must include 'src' so worktree tests import the working tree."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_pytest_pythonpath_includes_src() -> None:
    """Assert pyproject.toml sets [tool.pytest.ini_options].pythonpath to include 'src'.

    This ensures `python -m pytest` run from a git worktree imports that worktree's
    ./src rather than the main checkout's editable install .pth entry.
    """
    repo_root = Path(__file__).parent.parent
    pyproject = repo_root / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    pytest_opts = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
    pythonpath = pytest_opts.get("pythonpath", [])
    assert "src" in pythonpath, (
        f"[tool.pytest.ini_options].pythonpath must contain 'src' (got {pythonpath!r}). "
        "Without this, pytest in a git worktree imports the editable .pth from the main "
        "checkout instead of the worktree's ./src."
    )
