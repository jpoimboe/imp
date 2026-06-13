"""Detached imp subprocess spawners: resume, create, and abandon for the control plane."""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from imp.engine.env import overlay_engine_env


def _base_env() -> dict[str, str]:
    """os.environ minus engine-managed vars so stale values never reach the spawned imp."""
    env = overlay_engine_env(os.environ, {})
    env.pop("IMP_REMOTE_CONTROL_SOCK", None)
    return env


def _spawn_detached(argv: list[str], env: Mapping[str, str], cwd: Path) -> None:
    """Spawn a fully detached imp the daemon does not own (new session, DEVNULL I/O)."""
    subprocess.Popen(
        argv, env=dict(env), cwd=str(cwd),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)


def _imp(*args: str) -> list[str]:
    return [sys.executable, "-m", "imp", *args]


def resume_change(control_root: Path, change_id: str, text: str) -> None:
    """Deliver a reply to a Change paused on disk via `imp resume --message`."""
    _spawn_detached(
        _imp("resume", change_id, "--non-interactive", "--message", text),
        _base_env(), control_root)


def create_change(control_root: Path, prompt: str) -> None:
    _spawn_detached(
        _imp("new", "--non-interactive", "--message", prompt), _base_env(), control_root)


def abandon_change(control_root: Path, change_id: str) -> None:
    """Abandon a Change via `imp abandon --yes`."""
    _spawn_detached(_imp("abandon", change_id, "--yes"), _base_env(), control_root)
