"""Subprocess+PTY-driven test of the human gate path.

Catches the prompt_toolkit/asyncio loop conflict that pure sync-lambda
tests miss. Spawns `imp new` connected to a real pty, types "1\\n" at
the human gate, asserts the workflow completes.

Skips on Windows (no pty module) and on test runners without a /dev/tty
or where pty.openpty fails (some sandboxed CI envs).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest


def _can_pty() -> bool:
    try:
        import pty
        m, s = pty.openpty()
        os.close(m)
        os.close(s)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _can_pty(), reason="pty not available")


def _seed_zero_step_workflow(tmp_path: Path) -> None:
    wf = tmp_path / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "workflow.yaml").write_text(textwrap.dedent("""\
        name: t
        description: t
        phases: [only]
        defaults: {max_retries: 1, on_max_retries: {kind: pause}}
        only:
          steps: []
          gates:
            - type: human
              prompt: "pick one"
              options:
                - {label: yes, action: {kind: continue}}
                - {label: no, action: {kind: pause}}
    """))
    # No LLM calls happen (no steps), but the loader requires a default role.
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n"
        "  default: {prefer: [{provider: anthropic, model: claude-3-5-haiku-20241022}]}\n"
    )


def test_human_gate_via_real_pty(tmp_path: Path) -> None:
    """Real prompt_toolkit through a pty — would have caught the
    asyncio-loop conflict found during V1 dogfood (Plan 3 Task 21)."""
    import pty
    import subprocess

    _seed_zero_step_workflow(tmp_path)

    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "imp", "new", "--no-tui", "pty-test"],
            cwd=tmp_path,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)

        # Read until we see the human gate options, then send "1\n".
        deadline = time.time() + 30.0
        buf = b""
        while time.time() < deadline:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if b"1) yes" in buf:
                os.write(master, b"1\n")
                break
        else:
            proc.kill()
            proc.wait()
            pytest.fail(f"never saw human gate prompt; buf={buf!r}")

        rc = proc.wait(timeout=30)
        assert rc == 0, f"expected exit 0, got {rc}; buf={buf!r}"
    finally:
        with contextlib.suppress(OSError):
            os.close(master)
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    # Verify state completed.
    state_path = tmp_path / ".imp" / "changes" / "1" / "state" / "state.json"
    state = json.loads(state_path.read_text())
    assert state["status"] == "completed", f"expected completed, got {state['status']!r}"
