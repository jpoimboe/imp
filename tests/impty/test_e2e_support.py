from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.impty.e2e_support import seed_minimal_repo, write_imp_shim


def _scripted_env(script_path: Path) -> dict[str, str]:
    return {**os.environ, "IMP_SCRIPTED_SCRIPT": str(script_path), "IMP_THEME": "ansi"}


def test_seed_minimal_repo_runs_real_imp_new_to_a_scripted_side_effect(
    tmp_path: Path,
) -> None:
    sentinel = "IMP_DOGFOOD_supp01"
    script = {"default": [{"text": "working", "tools": [
        {"name": "bash", "input": {"command": (
            'cd "$IMP_PROJECT_ROOT" && mkdir -p src && '
            f'printf "# {sentinel}\\n" > src/marker.py && '
            'git add src/marker.py && git commit -qm "feat: marker"')}},
        {"name": "complete_step", "input": {"status": "Success"}},
    ]}]}
    script_path = seed_minimal_repo(tmp_path, script=script)

    proc = subprocess.run(
        [sys.executable, "-m", "imp", "new", "demo", "--non-interactive"],
        cwd=tmp_path, env=_scripted_env(script_path),
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr

    # The scripted bash ran as a REAL side effect on the change branch in the worktree.
    markers = list(tmp_path.glob(".imp/worktrees/*/src/marker.py"))
    assert markers, f"no committed scripted marker; stderr:\n{proc.stderr}"
    assert sentinel in markers[0].read_text()


def test_write_imp_shim_is_executable_and_runs_imp(tmp_path: Path) -> None:
    shim = write_imp_shim(tmp_path)
    assert os.access(shim, os.X_OK)
    out = subprocess.run([shim, "--help"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0
    assert "usage: imp" in (out.stdout + out.stderr)
