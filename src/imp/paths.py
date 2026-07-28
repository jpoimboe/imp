"""Where imp keeps state on disk: the per-machine home, and how a path is shown
to a user. Deliberately free of `imp.*` module-level imports — engine, CLI,
remote and provider modules all import it."""

from __future__ import annotations

import os
from pathlib import Path


def imp_home() -> Path:
    """$IMP_HOME, default ~/.imp — imp's per-machine directory."""
    base = os.environ.get("IMP_HOME") or str(Path.home() / ".imp")
    return Path(base)


def display_path(path: Path) -> str:
    """`~`-relative under $HOME, absolute otherwise."""
    home = Path.home()
    if home in path.parents:
        return f"~/{path.relative_to(home)}"
    return str(path)
