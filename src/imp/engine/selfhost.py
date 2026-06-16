"""Self-host launch for live drivers (imp-driving-imp)."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
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


def _prefixed(value: str | None, prefix: str) -> str:
    rest = [p for p in (value or "").split(os.pathsep) if p and p != prefix]
    return os.pathsep.join([prefix, *rest])


def selfhost_full_env(
    launch: SelfHostLaunch, environ: Mapping[str, str],
) -> dict[str, str]:
    """Complete replacement env for a driver's MCP server: IMP_* stripped, PATH/PYTHONPATH prefixed."""
    env = {k: v for k, v in environ.items() if not k.startswith("IMP_")}
    env["PATH"] = _prefixed(env.get("PATH"), launch.shim_dir)
    env["PYTHONPATH"] = _prefixed(env.get("PYTHONPATH"), launch.src_dir)
    return env


def selfhost_env_overrides(
    launch: SelfHostLaunch, environ: Mapping[str, str],
) -> dict[str, str]:
    """Just the PATH/PYTHONPATH overrides for a launcher that merges an env table."""
    # merge-table callers cannot unset inherited IMP_* vars
    return {
        "PATH": _prefixed(environ.get("PATH"), launch.shim_dir),
        "PYTHONPATH": _prefixed(environ.get("PYTHONPATH"), launch.src_dir),
    }
