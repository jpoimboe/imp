from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


@pytest.mark.timeout(30)
def test_pytest_timeout_is_configured_60s_signal() -> None:
    data = tomllib.loads(_PYPROJECT.read_text())
    ini = data["tool"]["pytest"]["ini_options"]
    assert ini["timeout"] == 60
    assert ini["timeout_method"] == "signal"
