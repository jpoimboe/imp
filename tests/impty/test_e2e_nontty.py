from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.impty.e2e_support import clean_env, seed_minimal_repo


def test_tui_falls_back_to_streaming_on_a_non_tty(tmp_path: Path) -> None:
    script = {"default": [{"text": "working", "tools": [
        {"name": "bash", "input": {"command": "echo hello-nontty"}},
        {"name": "complete_step", "input": {"status": "Success"}},
    ]}]}
    script_path = seed_minimal_repo(tmp_path, script=script)

    # stdout/stderr are PIPES (not a tty): --tui must degrade to streaming.
    proc = subprocess.run(
        [sys.executable, "-m", "imp", "new", "demo", "--tui", "--non-interactive"],
        cwd=tmp_path,
        env=clean_env(IMP_SCRIPTED_SCRIPT=str(script_path), IMP_THEME="ansi"),
        capture_output=True, text=True, timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    assert "streaming instead" in proc.stderr          # the fallback notice fired
    assert "\x1b[?1049h" not in proc.stdout            # never entered the alt screen
    assert "\x1b[?1049h" not in proc.stderr
