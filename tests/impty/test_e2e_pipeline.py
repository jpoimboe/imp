from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from impty import TerminalSession
from tests.impty.e2e_support import (
    can_pty,
    clean_env,
    default_workflow_script,
    seed_default_repo,
    write_imp_shim,
)

pytestmark = pytest.mark.skipif(not can_pty(), reason="pty not available")

_CHECKER = (Path(__file__).resolve().parents[1]
            / "playbooks" / "check_dogfood_invariants.py")


def test_scripted_default_workflow_run_passes_the_dogfood_invariants(
    tmp_path: Path,
) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    sentinel = "IMP_DOGFOOD_pipe01"
    script_path = seed_default_repo(clone, script=default_workflow_script(sentinel))
    base_sha = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True).stdout.strip()
    env = clean_env(IMP_SCRIPTED_SCRIPT=str(script_path), IMP_THEME="ansi")

    # Drive the real binary through impty to completion + merge.
    with TerminalSession(
        [sys.executable, "-m", "imp", "new", "--title", "demo change", "--non-interactive"],
        cwd=str(clone), env=env, cols=100, rows=40,
    ) as s:
        rc = s.run_to_completion(hard=180.0)
    assert rc == 0, "\n".join(s.lines())                  # completed + merged

    # The worktree is gone once the merge gate fired (completion finalized it).
    assert not list(clone.glob(".imp/worktrees/*")), "worktree not torn down (no merge?)"

    # Fold the unmodified invariants checker into the result.
    shim = write_imp_shim(tmp_path)
    checked = subprocess.run(
        [sys.executable, str(_CHECKER), str(clone), sentinel,
         "--base", base_sha, "--imp", shim],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert checked.returncode == 0, (
        f"dogfood invariants FAILED:\n{checked.stdout}\n{checked.stderr}")
    assert "RESULT: PASS" in checked.stdout
