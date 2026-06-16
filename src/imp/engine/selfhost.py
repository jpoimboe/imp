"""Self-host launch for live drivers (imp-driving-imp)."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from imp.engine.env import _is_imp_checkout

_SHIM_DIRNAME = "selfhost-bin"


@dataclass(frozen=True)
class SelfHostLaunch:
    shim_dir: str
    src_dir: str


def selfhost_launch(project_root: Path) -> SelfHostLaunch | None:
    """Return the self-host launch for an imp-checkout worktree, or None."""
    if not _is_imp_checkout(project_root):
        return None
    shim_dir = project_root / ".imp" / _SHIM_DIRNAME
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / "imp"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m imp "$@"\n')
    shim.chmod(0o755)
    return SelfHostLaunch(
        shim_dir=str(shim_dir), src_dir=str(project_root / "src"),
    )
