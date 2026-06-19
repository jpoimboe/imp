from __future__ import annotations

import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def test_pytest_xdist_listed_in_dev_extras() -> None:
    data = tomllib.loads(_PYPROJECT.read_text())
    dev = data["project"]["optional-dependencies"]["dev"]
    assert any(spec.startswith("pytest-xdist") for spec in dev), (
        f"pytest-xdist must be in [project.optional-dependencies].dev (got {dev!r}); "
        "without it the parallel `make test` has no scheduler plugin."
    )
