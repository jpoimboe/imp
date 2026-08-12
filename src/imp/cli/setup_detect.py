"""Lockfile-keyed detection of a project's worktree-setup command (suggestions only)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, NamedTuple


class DetectedSetupCommand(NamedTuple):
    """A detected worktree-setup command with the label the confirmation screen
    shows for the lockfile it was read from."""
    command: str
    provenance: str


# Only toolchains whose dependency directory is per-tree and gitignored. Go and Rust
# are deliberately absent: their module/crate caches are shared, so a fresh worktree
# needs no setup command at all.
_LOCKFILE_COMMANDS: tuple[tuple[str, str], ...] = (
    ("uv.lock", "uv sync --all-extras"),
    ("poetry.lock", "poetry install"),
    ("package-lock.json", "npm ci"),
    ("pnpm-lock.yaml", "pnpm install --frozen-lockfile"),
    ("yarn.lock", "yarn install"),
    ("Gemfile.lock", "bundle install"),
    ("mix.lock", "mix deps.get"),
    ("composer.lock", "composer install"),
)


def detect_worktree_setup(project_root: Path) -> DetectedSetupCommand | None:
    """A suggested command to run in each new quest worktree of `project_root` and
    where it came from, or None when no lockfile in the table is present. First
    match in table order wins."""
    for lockfile, command in _LOCKFILE_COMMANDS:
        if (project_root / lockfile).is_file():
            return DetectedSetupCommand(command, f"from {lockfile}")
    return None


def format_worktree_setup_yaml(command: str | Literal[False]) -> str:
    """`json.dumps` spells both states YAML accepts: a quoted command, or `false`."""
    return f"worktree_setup: {json.dumps(command)}\n"
