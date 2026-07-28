"""Where imp keeps state on disk: the per-machine home, and how a path is shown
to a user. Deliberately free of `imp.*` module-level imports — engine, CLI,
remote and provider modules all import it."""

from __future__ import annotations

import os
import re
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


_SLUG_DISALLOWED = re.compile(r"[^a-z0-9._-]")
_SLUG_MAX = 64


def _slug(basename: str) -> str:
    return _SLUG_DISALLOWED.sub("-", basename.lower())[:_SLUG_MAX] or "project"


def project_state_dir(control_root: Path) -> Path:
    """This project's state directory — where its quests, worktrees and archive
    live. Resolution claims: the first call for a control root creates the slug
    directory under `imp_home()/projects` and writes its `repo` marker."""
    root = control_root.resolve()
    candidate = imp_home() / "projects" / _slug(root.name)
    if candidate.exists():
        return candidate
    candidate.mkdir(parents=True)
    (candidate / "repo").write_text(f"{root}\n")
    return candidate
