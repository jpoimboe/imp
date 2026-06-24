from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_make_check_runs_uv_lock_check() -> None:
    """`make check` must invoke `uv lock --check` so a stale uv.lock fails the gate."""
    out = subprocess.run(
        ["make", "-n", "check"],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    assert "uv lock --check" in out
