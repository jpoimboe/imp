# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Josh Poimboeuf
"""Config-file locations. Stdlib-only, importing nothing beyond the equally
stdlib-only `imp.paths` leaf: the startup migration in `imp.__main__` runs before
any command dispatch, so resolving these paths must not drag in the yaml/provider
stack."""

from __future__ import annotations

import os
from pathlib import Path

from imp.paths import imp_home


def global_config_path() -> Path:
    """$IMP_HOME/config.yaml, default ~/.imp/config.yaml."""
    return imp_home() / "config.yaml"


def legacy_global_config_path() -> Path:
    """The pre-IMP_HOME location: $XDG_CONFIG_HOME/imp/config.yaml, default
    ~/.config/imp/config.yaml. Read only by the startup migration."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "imp" / "config.yaml"


def project_rename_pending(root: Path) -> bool:
    """`root` carries the pre-rename `imp/project.yaml` and no `imp/config.yaml`."""
    return ((root / "imp" / "project.yaml").is_file()
            and not (root / "imp" / "config.yaml").exists())
