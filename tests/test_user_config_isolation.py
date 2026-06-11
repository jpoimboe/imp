"""The autouse XDG isolation fixture (tests/conftest.py) repoints the user-config
layer at a per-test empty tmp dir so no test reads ~/.config/imp/config.yaml."""
from pathlib import Path

import pytest

from imp.providers.effective import user_config_path


def test_user_config_isolated_by_default(tmp_path: Path) -> None:
    assert user_config_path() == tmp_path / ".xdg-isolated" / "imp" / "config.yaml"
    assert not user_config_path().exists()


def test_local_xdg_override_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom = tmp_path / "custom-xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(custom))
    assert user_config_path() == custom / "imp" / "config.yaml"
