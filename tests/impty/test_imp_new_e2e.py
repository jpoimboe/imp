"""End-to-end: drive `imp new` through the impty harness to completion.

Reuses the no-LLM human-gate workflow seed from tests.test_human_gate_pty, so
no provider tokens are spent. pty-gated like the original."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from impty import TerminalSession
from tests._live import _await_worktree
from tests.test_human_gate_pty import _can_pty, _seed_human_gate_workflow

pytestmark = pytest.mark.skipif(not _can_pty(), reason="pty not available")


def test_drive_imp_new_to_completion(tmp_path: Path) -> None:
    _seed_human_gate_workflow(tmp_path)

    with TerminalSession(
        [sys.executable, "-m", "imp", "new", "pty-test"],
        cwd=str(tmp_path),
        cols=80,
        rows=24,
    ) as session:
        # Read until the human gate's options render, recognizing the prompt
        # from the rendered screen — exactly how an agent-arbiter would.
        deadline = time.time() + 30.0
        answered = False
        while time.time() < deadline:
            session.wait_idle(idle=0.3, hard=10.0)
            screen = session.viewport()
            if "1) yes" in screen:
                session.send("1\r")
                answered = True
                break
            if not session.is_alive():
                break
        assert answered, f"never saw the human gate; transcript=\n{session.transcript()}"

        rc = session.wait(timeout=30)
        assert rc == 0, f"expected exit 0, got {rc}; transcript=\n{session.transcript()}"

    # Completion is proven via state.json (below), NOT via transcript: nothing
    # reads the master after send("1\r"), so post-gate output is never fed to
    # pyte and transcript() is truncated at the gate. (Additionally, if
    # prompt_toolkit ever ran the gate on the alternate screen, history wouldn't
    # capture it either — buffer-swap fidelity is out of scope.) The end-to-end
    # *capture* this test proves is therefore viewport-based up to the gate; a
    # full-run drain + verdict is a later slice.

    worktree = _await_worktree(tmp_path, timeout=5.0)
    state_path = worktree / ".imp" / "change" / "state" / "state.json"
    state = json.loads(state_path.read_text())
    assert state["status"] == "completed", f"expected completed, got {state['status']!r}"
