from __future__ import annotations

import signal
import sys
from pathlib import Path

import pytest

from impty import TerminalSession
from tests.impty.e2e_support import can_pty, clean_env, seed_minimal_repo
from tests.impty.matchers import assert_screen

pytestmark = pytest.mark.skipif(not can_pty(), reason="pty not available")


def test_midrun_sigint_pauses_with_exit_130_and_a_resume_hint(tmp_path: Path) -> None:
    script = {"default": [{"text": "working", "tools": [
        # echo then a long sleep: the step is reliably mid-run when SIGINT lands.
        {"name": "bash", "input": {"command": "echo running; sleep 30"}},
        {"name": "complete_step", "input": {"status": "Success"}},
    ]}]}
    script_path = seed_minimal_repo(tmp_path, script=script)
    env = clean_env(IMP_SCRIPTED_SCRIPT=str(script_path), IMP_THEME="ansi")

    with TerminalSession(
        [sys.executable, "-m", "imp", "new", "demo", "--non-interactive"],
        cwd=str(tmp_path), env=env, cols=80, rows=24,
    ) as s:
        # The step has started and the scripted bash is now sleeping → output
        # settles. Settling (not a fixed timer) guarantees we interrupt mid-step.
        s.wait_idle(idle=0.4, hard=12.0)
        s.send_signal(signal.SIGINT)
        rc = s.run_to_completion(hard=20.0)

        assert rc == 130                                   # interrupt → exit 130
        assert_screen(s, contains=("Paused", "imp resume"))  # paused + resumable
