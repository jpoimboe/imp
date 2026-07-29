"""One-shot startup migration of a project's runtime state out of the checkout and
into its state directory. The only reader of the pre-migration layout left."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def legacy_state_dir(control_root: Path) -> Path:
    """Where per-project state lived before this migration."""
    return control_root / ".imp"


def _same_device(a: Path, b: Path) -> bool:
    """Module-level so the cross-device branch is reachable on a single-fs tmpdir."""
    return a.stat().st_dev == b.stat().st_dev


def _discard(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _move_entries(legacy: Path, state_dir: Path) -> None:
    """Relocate each top-level entry, resumably: a destination that already exists
    was moved by an earlier run, and the source is never touched until the
    destination is in place — so a partial staging dir is always safe to discard."""
    same = _same_device(legacy, state_dir)
    for entry in sorted(legacy.iterdir()):
        dest = state_dir / entry.name
        if dest.exists():
            _discard(entry)
            continue
        if same:
            os.rename(entry, dest)
            continue
        staging = state_dir / f".migrating-{entry.name}"
        _discard(staging)
        if entry.is_dir() and not entry.is_symlink():
            shutil.copytree(entry, staging, symlinks=True)
        else:
            shutil.copy2(entry, staging, follow_symlinks=False)
        os.rename(staging, dest)
        _discard(entry)
