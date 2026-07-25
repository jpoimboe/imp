"""One-shot startup migrations, run by the console-script entrypoint
(`imp.__main__:main`) and never by `imp.cli.main:main` — the suite drives the
latter in-process, where a migration would rearrange the developer's own files."""

from __future__ import annotations

import shutil
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


def run_startup_migrations() -> None:
    """Best-effort: a migration that cannot run is retried on the next
    invocation and must never be the reason a command dies."""
    try:
        from imp.providers.effective import (
            global_config_path,
            legacy_global_config_path,
        )
        migrate_global_config(legacy_global_config_path(), global_config_path())
    except Exception:
        pass
