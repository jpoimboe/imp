# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Config-file locations."""

from __future__ import annotations

from pathlib import Path

from imp.paths import imp_home


def global_config_path() -> Path:
    """$IMP_HOME/config.yaml, default ~/.imp/config.yaml."""
    return imp_home() / "config.yaml"


def project_rename_pending(root: Path) -> bool:
    """`root` carries the pre-rename `imp/project.yaml` and no `imp/config.yaml`."""
    return ((root / "imp" / "project.yaml").is_file()
            and not (root / "imp" / "config.yaml").exists())
