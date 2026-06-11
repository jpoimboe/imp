"""The autouse XDG isolation fixture (tests/conftest.py) repoints the user-config
layer at a per-test empty tmp dir so no test reads ~/.config/imp/config.yaml."""
from pathlib import Path

from imp.providers.effective import user_config_path


def test_user_config_isolated_by_default(tmp_path: Path) -> None:
    assert user_config_path() == tmp_path / ".xdg-isolated" / "imp" / "config.yaml"
    assert not user_config_path().exists()
