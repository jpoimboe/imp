"""Grep guard: enforce the public-attribute contract from Plan 1 review #7.

`Provider.model` is part of the protocol; tests/engine/runners must not reach
through to a private `_model` attribute. This test fails loud if any commit
re-introduces the pattern.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def test_no_provider_model_reachthrough() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    this_file = Path(__file__).name
    proc = subprocess.run(
        ["grep", "-rn", "-E",
         "--exclude", this_file,
         r"provider\._model|self\._model\b|\._model\b",
         str(repo_root / "src/imp"), str(repo_root / "tests")],
        capture_output=True, text=True, check=False,
    )
    # grep returns 1 when no matches (which is what we want).
    if proc.returncode == 0:
        offending = [
            line for line in proc.stdout.splitlines()
            if line.strip() and "_model" in line and not line.startswith("#")
        ]
        assert not offending, (
            "Private model attribute reach-through detected (use provider.model):\n"
            + "\n".join(offending)
        )
