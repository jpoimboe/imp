from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def test_pytest_xdist_listed_in_dev_extras() -> None:
    data = tomllib.loads(_PYPROJECT.read_text())
    dev = data["project"]["optional-dependencies"]["dev"]
    assert any(spec.startswith("pytest-xdist") for spec in dev), (
        f"pytest-xdist must be in [project.optional-dependencies].dev (got {dev!r}); "
        "without it the parallel `make test` has no scheduler plugin."
    )


def _make_dry_run(*goals: str) -> str:
    """Expanded recipe text for `make -n <goals>` from the repo root (no execution)."""
    out = subprocess.run(
        ["make", "-n", *goals],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True,
    )
    return out.stdout


def test_make_test_is_parallel_by_default() -> None:
    pytest.importorskip("xdist")
    recipe = _make_dry_run("test")
    assert "--dist loadgroup" in recipe
    assert re.search(r"-n \S+", recipe), f"expected an -n worker flag in: {recipe!r}"


def test_make_test_verbose_is_parallel_by_default() -> None:
    pytest.importorskip("xdist")
    recipe = _make_dry_run("test-verbose")
    assert "--dist loadgroup" in recipe
    assert re.search(r"-n \S+", recipe), f"expected an -n worker flag in: {recipe!r}"


def test_make_test_runs_serial_when_xdist_overridden_empty() -> None:
    pytest.importorskip("xdist")
    recipe = _make_dry_run("test", "XDIST=")
    assert "--dist" not in recipe
    assert not re.search(r"-n \S+", recipe), f"expected no -n flag in: {recipe!r}"
