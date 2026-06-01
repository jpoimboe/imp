"""Helpers for parsing IMP_LIVE_PROVIDERS in live-API tests.

Live tests opt in via the IMP_LIVE_PROVIDERS env var (comma-separated).
Each test's skipif checks `opted_in(<provider>)`. Auth presence is then
asserted (not skipped) — opt-in implies the user has set up credentials.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


def _parse() -> list[str]:
    raw = os.environ.get("IMP_LIVE_PROVIDERS", "")
    return [tok.strip() for tok in raw.split(",") if tok.strip()]


def opted_in(provider: str) -> bool:
    return provider in _parse()


def first_opted_in() -> str | None:
    items = _parse()
    return items[0] if items else None


def _await_worktree(control_root: Path, timeout: float) -> Path:
    """Poll .imp/worktrees/ for the single Change worktree dir and return it;
    its .name is the slug. The real guard is the presence of
    .imp/change/change.json in the candidate (scratch/.merge-* dirs lack it);
    the leading-dot exclusion is redundant belt-and-suspenders for readability.

    Replaces stdout-parsing of `imp new`'s "Created Change <slug>:" line:
    sidesteps PTY/PIPE read-timing and yields the slug directly. Raises
    TimeoutError if no Change worktree appears within `timeout` seconds."""
    root = control_root / ".imp" / "worktrees"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if root.is_dir():
            for p in sorted(root.iterdir()):
                if (p.is_dir() and not p.name.startswith(".")
                        and (p / ".imp" / "change" / "change.json").exists()):
                    return p
        time.sleep(0.1)
    raise TimeoutError(
        f"no Change worktree appeared under {root} within {timeout}s"
    )
