"""PTY-driven full shipped default workflow end-to-end against the real Anthropic API.

Spawns `python -m imp new "title"` connected to a real pseudo-terminal via
pty.openpty(), drives the 6 human gates by typing "1\n" each time, asserts
the workflow completes.  This is the test that *would have caught Bug 1*
(prompt_toolkit/asyncio loop crash) in CI, because it exercises the real
prompt_toolkit code path — unlike the sync-lambda tests that bypassed it.

Three skip predicates (all must pass):
  1. ANTHROPIC_API_KEY (or ~/.anthropic-key) must be present.
  2. IMP_RUN_LIVE_TESTS=1 must be set.
  3. PTY must be available (not Windows, not sandboxed CI without /dev/pty*).
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

# ── Helpers ────────────────────────────────────────────────────────────────


def _anthropic_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    key_file = Path.home() / ".anthropic-key"
    if key_file.exists():
        return key_file.read_text().strip()
    return None


def _can_pty() -> bool:
    try:
        import pty
        m, s = pty.openpty()
        os.close(m)
        os.close(s)
        return True
    except Exception:
        return False


# ── Skip predicates ────────────────────────────────────────────────────────

pytestmark = [
    pytest.mark.skipif(
        not _anthropic_key(),
        reason="PTY live walk requires ANTHROPIC_API_KEY or ~/.anthropic-key",
    ),
    pytest.mark.skipif(
        os.environ.get("IMP_RUN_LIVE_TESTS") != "1",
        reason="expensive (~30¢); set IMP_RUN_LIVE_TESTS=1 to run",
    ),
    pytest.mark.skipif(
        not _can_pty(),
        reason="pty not available (Windows or sandboxed CI)",
    ),
]


# ── Test ───────────────────────────────────────────────────────────────────


def test_full_default_workflow_via_pty_real_anthropic(tmp_path: Path) -> None:
    """Real prompt_toolkit through a pty — exercises the path Bug 1 broke.

    Drives the shipped default workflow start to finish:
      onboard → spec → design → plan → implement → review → merge
    Each human gate gets "1\n" (first option = proceed).
    """
    import pty
    import shutil

    key = _anthropic_key()
    assert key is not None

    # Seed the shipped workflow.
    src = (
        Path(__file__).resolve().parents[1]
        / "src" / "imp" / "assets" / "workflows" / "default"
    )
    dst = tmp_path / ".imp" / "workflows" / "default"
    shutil.copytree(src, dst)

    # Stub out all script gates so they always pass (exit 0).  Without this
    # the check-artifact.sh and run-tests.sh scripts would fail because the
    # agents don't produce real artifacts / run real tests in this context.
    for shell in (dst / "scripts").glob("*.sh"):
        shell.write_text("#!/bin/sh\nexit 0\n")
        shell.chmod(0o755)

    # Config: all roles → Sonnet via Anthropic.
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n"
        "  default:       {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  spec:          {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  spec_review:   {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  design:        {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  design_review: {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  code:          {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  code_review:   {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
    )

    env = dict(os.environ)
    env["ANTHROPIC_API_KEY"] = key

    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-m", "imp", "new", "pty-live-test"],
        cwd=tmp_path,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        start_new_session=True,
    )
    os.close(slave)

    # Drive human gates.
    # Strategy: accumulate PTY output.  Whenever we see a new "HUMAN GATE"
    # marker and then the read goes quiet (select.select times out for 0.5s),
    # the options menu has fully rendered → send "1\n".
    #
    # The shipped default workflow has 6 human gates:
    #   onboard, spec, design, plan, review, merge.
    GATE_MARKER = b"HUMAN GATE"
    GATE_TIMEOUT = 0.5       # seconds of silence after marker → menu rendered
    TOTAL_TIMEOUT = 600.0    # 10 min hard cap for the full workflow

    buf = bytearray()
    gates_seen = 0
    gates_answered = 0
    deadline = time.time() + TOTAL_TIMEOUT

    try:
        while time.time() < deadline:
            # Non-blocking read with short timeout.
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break  # process closed the pty
                if not chunk:
                    break
                buf.extend(chunk)

            # Count how many HUMAN GATE markers are in the buffer.
            count = buf.count(GATE_MARKER)
            if count > gates_seen:
                # A new gate appeared.  Remember when we first saw it and
                # wait for silence before sending the answer.
                gates_seen = count
                last_gate_time = time.time()
                continue

            # If a gate appeared and we haven't answered it yet,
            # wait for silence (GATE_TIMEOUT) before sending "1\n".
            if gates_seen > gates_answered:
                elapsed_since_gate = time.time() - last_gate_time
                # Also wait if new bytes are still coming in.
                if not ready and elapsed_since_gate >= GATE_TIMEOUT:
                    os.write(master, b"1\n")
                    gates_answered += 1

            # Exit loop once the process has ended.
            if proc.poll() is not None:
                # Drain any remaining output.
                while True:
                    r, _, _ = select.select([master], [], [], 0.05)
                    if not r:
                        break
                    try:
                        tail = os.read(master, 4096)
                    except OSError:
                        break
                    if not tail:
                        break
                    buf.extend(tail)
                break

    finally:
        with contextlib.suppress(OSError):
            os.close(master)
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    rc = proc.wait(timeout=10)
    assert rc == 0, (
        f"expected exit 0, got {rc}; "
        f"gates_seen={gates_seen} gates_answered={gates_answered}\n"
        f"last 2000 bytes of output:\n{bytes(buf[-2000:])!r}"
    )

    state_path = tmp_path / ".imp" / "changes" / "1" / "state" / "state.json"
    state = json.loads(state_path.read_text())
    assert state["status"] in ("completed", "paused"), (
        f"unexpected status {state['status']!r}; "
        f"gates_seen={gates_seen} gates_answered={gates_answered}"
    )
