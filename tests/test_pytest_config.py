"""Locking test: pytest pythonpath must include 'src' so worktree tests import the working tree."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path


def test_subprocess_imp_resolves_to_working_tree() -> None:
    """A child `python -c "import imp"` must resolve `imp` from THIS checkout's
    `src`, not the shared global editable install.

    pyproject's `pythonpath=["src"]` only covers the in-process interpreter;
    subprocess-spawning tests resolve `imp` from the global editable install,
    which parallel worktrees/sessions tear down or repoint. conftest pins
    PYTHONPATH so this holds under bare `pytest`, not only `make test`. Regression
    guard for that footgun: if conftest's pin regresses, this fails directly
    instead of surfacing as a confusing ModuleNotFoundError in unrelated tests.
    """
    expected_src = (Path(__file__).resolve().parent.parent / "src").resolve()
    out = subprocess.run(
        [sys.executable, "-c", "import imp, sys; sys.stdout.write(imp.__file__ or '')"],
        capture_output=True, text=True, check=True,
    )
    resolved = out.stdout.strip()
    assert resolved, (
        "subprocess `import imp` resolved a namespace package (imp.__file__ is None) "
        "— the global editable install is torn down and PYTHONPATH was not pinned to "
        "the working tree. conftest's subprocess-imp pin is missing or broken."
    )
    assert Path(resolved).resolve().is_relative_to(expected_src), (
        f"subprocess resolved imp from {resolved}, expected under {expected_src}. "
        "The shared editable install shadowed the working tree — conftest's "
        "PYTHONPATH pin is missing or broken."
    )


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
