"""Resolve imp's own shipped project config file by name.

imp's `imp/project.yaml` is renamed by the startup migration's staged `git mv`
in the main checkout, not by this repo's own diff, so the shipped file answers
to either name for one commit's worth of window."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def repo_config_file() -> Path:
    new = REPO_ROOT / "imp" / "config.yaml"
    return new if new.is_file() else REPO_ROOT / "imp" / "project.yaml"
