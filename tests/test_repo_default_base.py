from __future__ import annotations

from pathlib import Path

from imp.providers.config import parse_default_base


def test_this_repo_configures_master_as_default_base() -> None:
    repo_config = Path(__file__).resolve().parents[1] / "imp" / "config.yaml"
    assert parse_default_base(repo_config) == "master"
