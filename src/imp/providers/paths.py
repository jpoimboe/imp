"""Config-file locations. Stdlib-only and free of `imp` imports: the startup
migration in `imp.__main__` runs before any command dispatch, so resolving these
paths must not drag in the yaml/provider stack."""

from __future__ import annotations

import os
from pathlib import Path


def imp_home() -> Path:
    """$IMP_HOME, default ~/.imp — imp's per-machine directory."""
    base = os.environ.get("IMP_HOME") or str(Path.home() / ".imp")
    return Path(base)


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
