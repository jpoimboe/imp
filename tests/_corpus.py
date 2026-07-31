"""Corpus scanners read git-tracked files only: an untracked scratch note under
docs/ is not shipped content and must not fail the suite."""
from __future__ import annotations

import subprocess
from pathlib import Path


def tracked(*roots: str, suffixes: tuple[str, ...] = (".md",)) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z", "--", *roots],
                         capture_output=True, text=True, check=True).stdout
    files = [Path(p) for p in out.split("\0") if p.endswith(suffixes)]
    return sorted(p for p in files if p.is_file())
