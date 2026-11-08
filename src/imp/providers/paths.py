# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Config-file and provider-credential locations."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from imp.paths import imp_home


def global_config_path() -> Path:
    """$IMP_HOME/config.yaml, default ~/.imp/config.yaml."""
    return imp_home() / "config.yaml"


def project_rename_pending(root: Path) -> bool:
    """`root` carries the pre-rename `imp/project.yaml` and no `imp/config.yaml`."""
    return ((root / "imp" / "project.yaml").is_file()
            and not (root / "imp" / "config.yaml").exists())


def claude_canonical_dir(env: Mapping[str, str]) -> Path:
    override = env.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".claude"


def codex_canonical_dir(env: Mapping[str, str]) -> Path:
    override = env.get("CODEX_HOME")
    return Path(override) if override else Path.home() / ".codex"
