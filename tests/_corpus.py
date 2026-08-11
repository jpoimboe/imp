"""Corpus scanners read git-tracked files only: an untracked scratch note under
docs/ is not shipped content and must not fail the suite."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _ls_files(*roots: str) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z", "--", *roots],
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\0") if p]


def tracked(*roots: str, suffixes: tuple[str, ...] = (".md",)) -> list[Path]:
    files = [Path(p) for p in _ls_files(*roots) if p.endswith(suffixes)]
    return sorted(p for p in files if p.is_file())


def tracked_paths() -> set[str]:
    return set(_ls_files())


def relative_link_targets(text: str) -> list[str]:
    return [t for t in _LINK.findall(text)
            if not t.startswith(("http://", "https://", "#"))]
