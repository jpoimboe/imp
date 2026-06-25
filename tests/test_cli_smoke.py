# tests/test_cli_smoke.py
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_imp_status_runs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".imp").mkdir()
    # Run via -m so we exercise the entrypoint
    r = subprocess.run(
        [sys.executable, "-m", "imp", "show"],
        capture_output=True, text=True, check=False,
    )
    # Either prints empty status or an "no changes" message — either is fine
    assert r.returncode == 0 or "no changes" in (r.stdout + r.stderr).lower()
