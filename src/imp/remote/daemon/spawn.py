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


def _run_foreground(argv: list[str], env: Mapping[str, str], cwd: Path) -> int:
    """Run imp synchronously (the daemon owns it), discarding I/O; returns its exit code."""
    return subprocess.run(
        argv, env=dict(env), cwd=str(cwd),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL).returncode


def _imp(*args: str) -> list[str]:
    return [sys.executable, "-m", "imp", *args]


def create_quest(control_root: Path, prompt: str) -> None:
    env = _base_env()
    env["IMP_REMOTE_AUTOSUBSCRIBE"] = "1"
    _spawn_detached(
        _imp("new", "--non-interactive", "--message", prompt), env, control_root)


def resume_quest(control_root: Path, token: str) -> str:
    """Resolve a token to a single quest and spawn a detached `imp resume` for it."""
    from imp.cli.main import _iter_quest_ids
    matches = [cid for cid in _iter_quest_ids(control_root) if token in cid]
    if len(matches) != 1:
        if not matches:
            return f"No quest matches {token!r}."
        return f"{token!r} is ambiguous: {', '.join(matches)}"
    target = matches[0]
    env = _base_env()
    env["IMP_REMOTE_AUTOSUBSCRIBE"] = "1"
    _spawn_detached(
        _imp("resume", target, "--non-interactive"), env, control_root)
    return f"Resuming @{target}…"


def start_quest(control_root: Path, quest_id: str) -> None:
    """Spawn a detached, non-interactive, autonomous `imp resume` for a started draft."""
    _spawn_detached(
        _imp("resume", quest_id, "--non-interactive", "--autonomous"),
        _base_env(), control_root)


def abandon_quest(control_root: Path, quest_id: str) -> int:
    """Abandon a quest via `imp abandon`, synchronously; returns its exit code."""
    return _run_foreground(
        _imp("abandon", quest_id), _base_env(), control_root)
