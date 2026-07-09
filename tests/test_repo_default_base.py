from __future__ import annotations

from pathlib import Path

from imp.providers.config import parse_default_base


def test_this_repo_configures_master_as_default_base() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    assert parse_default_base(repo_root / "imp" / "project.yaml") == "master"
