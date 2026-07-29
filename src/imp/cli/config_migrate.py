"""One-shot startup migrations: the global config file's move into $IMP_HOME and
the project config file's staged rename. Run by the console-script entrypoint
(`imp.__main__:main`), never by `imp.cli.main:main` — the suite drives the latter
in-process, where a migration would rearrange the developer's own files."""

from __future__ import annotations

import shutil
import subprocess
import sys
from contextlib import suppress
from pathlib import Path


def display_path(path: Path) -> str:
    """`~`-relative under $HOME, absolute otherwise."""
    home = Path.home()
    if home in path.parents:
        return f"~/{path.relative_to(home)}"
    return str(path)


def migrate_global_config(legacy: Path, new: Path) -> None:
    """Move a pre-IMP_HOME global config into $IMP_HOME, once."""
    if not legacy.is_file() or new.exists():
        return
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(legacy), str(new))  # not os.replace: the two may differ in fs
    with suppress(OSError):
        legacy.parent.rmdir()
    print(f"imp: moved {display_path(legacy)} to {display_path(new)}",
          file=sys.stderr)


def _git_toplevel(cwd: Path) -> Path | None:
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip()).resolve()


def migrate_project_config(cwd: Path) -> None:
    """Stage `imp/project.yaml` -> `imp/config.yaml` in the main checkout, once."""
    from imp.engine.worktree import find_control_root
    from imp.providers.paths import project_rename_pending

    # Pure-filesystem pre-check: an already-migrated repo is the permanent steady
    # state, and must not pay a git spawn on every imp invocation.
    if not any(project_rename_pending(p) for p in (cwd, *cwd.parents)):
        return
    control = find_control_root(cwd)
    if not project_rename_pending(control):
        return
    # In a linked worktree the toplevel is the worktree and the control root the
    # main checkout, so this inequality IS the mid-run guard.
    if _git_toplevel(cwd) != control:
        return
    subprocess.run(["git", "mv", "imp/project.yaml", "imp/config.yaml"],
                   cwd=control, capture_output=True, text=True, check=True)
    print("imp: renamed imp/project.yaml to imp/config.yaml (staged) — "
          "commit it to finish the move", file=sys.stderr)


def run_startup_migrations() -> None:
    """Best-effort per step: a migration that cannot run is retried on the next
    invocation and must never be the reason a command dies."""
    with suppress(Exception):
        from imp.providers.paths import (
            global_config_path,
            legacy_global_config_path,
        )
        migrate_global_config(legacy_global_config_path(), global_config_path())
    with suppress(Exception):
        migrate_project_config(Path.cwd())
