"""Walk-up search for the nearest `.imp/` directory.

`imp` is project-local; every command beyond `init` needs to find the root
of the current project. Mirrors how `git` walks up looking for `.git/`.

If no `.imp/` is found, returns `start` unchanged — `imp init` runs in
"no project yet" mode and creates one in cwd.
"""

from __future__ import annotations

from pathlib import Path


def find_project_root(start: Path | None = None) -> Path:
    cur = (start or Path.cwd()).resolve()
    while True:
        if (cur / ".imp").is_dir():
            return cur
        if cur.parent == cur:  # filesystem root
            return (start or Path.cwd()).resolve()
        cur = cur.parent
