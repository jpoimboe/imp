"""The autouse isolation fixture (tests/conftest.py) repoints the global config
layer at a per-test empty tmp dir so no test reads ~/.imp/config.yaml."""
from pathlib import Path

import pytest

from imp.providers.paths import global_config_path


def test_global_config_isolated_by_default(tmp_path: Path) -> None:
    assert global_config_path() == tmp_path / ".imp-isolated" / "config.yaml"
    assert not global_config_path().exists()


def test_local_imp_home_override_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom = tmp_path / "custom-home"
    monkeypatch.setenv("IMP_HOME", str(custom))
    assert global_config_path() == custom / "config.yaml"
