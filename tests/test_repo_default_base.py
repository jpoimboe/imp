from __future__ import annotations

from imp.providers.config import parse_default_base
from tests._repo_config import repo_config_file


def test_this_repo_configures_master_as_default_base() -> None:
    assert parse_default_base(repo_config_file()) == "master"
