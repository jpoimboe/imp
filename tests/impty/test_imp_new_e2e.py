"""End-to-end: drive `imp new` through the impty harness to completion.

Reuses the no-LLM human-gate workflow seed from tests.test_human_gate_pty, so
no provider tokens are spent. pty-gated like the original."""

from __future__ import annotations

import json
import os
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

    # The subprocess (`python -m imp`) runs in tmp_path and only inherits env,
    # not pytest's in-process sys.path. Make it self-sufficient by prepending
    # this checkout's src/ to PYTHONPATH so `imp` resolves to src/imp regardless
    # of how pytest was invoked (bare `pytest` vs `make test`) and despite the
    # repo-root `imp/` dogfood dir that otherwise shadows it as a namespace pkg.
    src_dir = Path(__file__).resolve().parents[2] / "src"
    ambient = os.environ.get("PYTHONPATH", "")
    pythonpath = f"{src_dir}{os.pathsep}{ambient}" if ambient else str(src_dir)
    env = {**os.environ, "PYTHONPATH": pythonpath}

    with TerminalSession(
        [sys.executable, "-m", "imp", "new", "pty-test"],
        cwd=str(tmp_path),
        env=env,
        cols=80,
        rows=24,
    ) as session:
        # Read until the human gate's options render, recognizing the prompt
        # from the rendered screen — exactly how an agent-arbiter would.
        deadline = time.time() + 30.0
        answered = False
        while time.time() < deadline:
            session.wait_idle(idle=0.3, hard=10.0)
            screen = "\n".join(session.lines(limit=session.rows))
            if "1) yes" in screen:
                session.send("1\r")
                answered = True
                break
            if not session.is_alive():
                break
        assert answered, "never saw the human gate; lines=\n" + "\n".join(session.lines())

        # Drain post-gate output into pyte AND reap. Now that the harness reads
        # the master to completion, the verdict can come from lines().
        rc = session.run_to_completion(hard=30.0)
        assert rc == 0, (
            f"expected exit 0, got {rc}; lines=\n" + "\n".join(session.lines())
        )

        # The gate renders inline today (no alternate screen), so post-gate
        # output lands in pyte history (scroll shape). This assert is a tripwire:
        # if a future step entered alt-screen and never restored it, the regime
        # would be SCREEN and the scrollback verdict below would be unreliable.
        assert session.is_alt_screen() is False
        # Primary verdict: the completion banner `Change <id>: completed`
        # (src/imp/cli/main.py:172).
        assert ": completed" in "\n".join(session.lines()), (
            "no completion banner in lines=\n" + "\n".join(session.lines())
        )

    # Backstop: state.json still records completion authoritatively.
    worktree = _await_worktree(tmp_path, timeout=5.0)
    state_path = worktree / ".imp" / "change" / "state" / "state.json"
    state = json.loads(state_path.read_text())
    assert state["status"] == "completed", f"expected completed, got {state['status']!r}"
