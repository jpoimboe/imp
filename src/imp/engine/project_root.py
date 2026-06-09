"""Walk-up search for the nearest `imp/project.yaml` marker file.

`imp` is project-local; every command beyond `init` needs to find the root
of the current project. Mirrors how `git` walks up looking for `.git/`.

If no `imp/project.yaml` is found, returns `start` resolved — `imp init` runs
in "no project yet" mode and creates one in cwd.
"""

from __future__ import annotations

from pathlib import Path


def find_project_root(start: Path | None = None) -> Path:
    """Cheap imp/project.yaml marker-walk (no git) for `imp init` discovery and tests.

    This is distinct from worktree.find_control_root, which is the
    git-authoritative resolver used for running-engine paths.
    """
    resolved_start = (start or Path.cwd()).resolve()
    cur = resolved_start
    while True:
        if (cur / "imp" / "project.yaml").is_file():
            return cur
        if cur.parent == cur:  # filesystem root
            return resolved_start
        cur = cur.parent
