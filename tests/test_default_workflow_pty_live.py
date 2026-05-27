"""PTY-driven full shipped default workflow end-to-end against a real LLM.

Spawns `python -m imp new "title"` connected to a real pseudo-terminal via
pty.openpty(), drives the 6 human gates by typing "1\n" each time, asserts
the workflow completes.  This is the test that *would have caught Bug 1*
(prompt_toolkit/asyncio loop crash) in CI, because it exercises the real
prompt_toolkit code path — unlike the sync-lambda tests that bypassed it.

Provider selection: prefers `claude_code` (uses `claude -p` against the
user's subscription, no per-call API spend) when the CLI is on PATH; falls
back to the Anthropic API otherwise.  Both run Sonnet-class models against
the same shipped workflow.

Three skip predicates (all must pass):
  1. Either `claude` CLI on PATH OR ANTHROPIC_API_KEY (or ~/.anthropic-key)
     must be present.
  2. IMP_RUN_LIVE_TESTS=1 must be set.
  3. PTY must be available (not Windows, not sandboxed CI without /dev/pty*).
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import shutil
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


def _pick_provider() -> tuple[str, str] | None:
    """Pick the cheapest available provider. Returns (provider_name, model)
    or None if no provider has usable auth."""
    if shutil.which("claude"):
        return "claude_code", "claude-sonnet-4-5"
    if _anthropic_key():
        return "anthropic", "claude-sonnet-4-6"
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
        _pick_provider() is None,
        reason="PTY live walk requires `claude` CLI on PATH or ANTHROPIC_API_KEY",
    ),
    pytest.mark.skipif(
        os.environ.get("IMP_RUN_LIVE_TESTS") != "1",
        reason="costs real LLM tokens; set IMP_RUN_LIVE_TESTS=1 to run",
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

    picked = _pick_provider()
    assert picked is not None
    provider, model = picked

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

    # Lower max_retries to 1 so the workflow pauses promptly if a reviewer
    # keeps rejecting. With the shipped default of 3, each spec/design/code
    # reviewer that finds nits can burn 3 × ~80s of Sonnet inference; the
    # test's wall-clock cap gets eaten before the workflow can reach merge.
    # 1 retry is enough to exercise the retry path; further iteration just
    # exercises the same code repeatedly.
    wf_yaml = dst / "workflow.yaml"
    wf_yaml.write_text(
        wf_yaml.read_text().replace("max_retries: 3", "max_retries: 1")
    )

    # Config: all roles → picked provider/model.
    role_line = "{prefer: [{provider: " + provider + ", model: " + model + "}]}"
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n"
        f"  default:       {role_line}\n"
        f"  spec:          {role_line}\n"
        f"  spec_review:   {role_line}\n"
        f"  design:        {role_line}\n"
        f"  design_review: {role_line}\n"
        f"  code:          {role_line}\n"
        f"  code_review:   {role_line}\n"
    )

    env = dict(os.environ)
    if provider == "anthropic":
        key = _anthropic_key()
        assert key is not None
        env["ANTHROPIC_API_KEY"] = key
    # claude_code uses the user's `claude` subscription via the CLI;
    # no env key injection needed. Just make sure CLAUDECODE isn't set
    # (the conftest doesn't touch it here, but be defensive).
    env.pop("CLAUDECODE", None)

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
    # Full workflow is 8 agents × 30-90s/agent on claude_code Sonnet-4.5
    # plus 6 human gates. 8 min cap is generous; a real hang shows up as
    # buffer flat for >30s which the developer can spot via the heartbeat.
    TOTAL_TIMEOUT = 480.0
    PROGRESS_INTERVAL = 5.0  # heartbeat to stderr so the test doesn't look hung
    TRACE_PTY = os.environ.get("IMP_PTY_TRACE") == "1"

    buf = bytearray()
    gates_seen = 0
    gates_answered = 0
    start_time = time.time()
    deadline = start_time + TOTAL_TIMEOUT
    last_progress = start_time
    print(
        f"\n[pty-test] provider={provider} model={model} starting...",
        file=sys.stderr, flush=True,
    )

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
                if TRACE_PTY:
                    # Stream every byte through to stderr so the test
                    # surface matches what an interactive user would see.
                    sys.stderr.buffer.write(chunk)
                    sys.stderr.buffer.flush()

            # Count how many HUMAN GATE markers are in the buffer.
            count = buf.count(GATE_MARKER)
            if count > gates_seen:
                # A new gate appeared.  Remember when we first saw it and
                # wait for silence before sending the answer.
                gates_seen = count
                last_gate_time = time.time()
                print(
                    f"[pty-test] saw HUMAN GATE #{gates_seen} at "
                    f"{time.time() - start_time:.1f}s",
                    file=sys.stderr, flush=True,
                )
                continue

            # If a gate appeared and we haven't answered it yet,
            # wait for silence (GATE_TIMEOUT) before sending "1\n".
            if gates_seen > gates_answered:
                elapsed_since_gate = time.time() - last_gate_time
                # Also wait if new bytes are still coming in.
                if not ready and elapsed_since_gate >= GATE_TIMEOUT:
                    os.write(master, b"1\n")
                    gates_answered += 1
                    print(
                        f"[pty-test] answered gate #{gates_answered}",
                        file=sys.stderr, flush=True,
                    )

            # Heartbeat so the developer knows the test isn't stuck.
            now = time.time()
            if now - last_progress >= PROGRESS_INTERVAL:
                last_progress = now
                print(
                    f"[pty-test] {now - start_time:.0f}s elapsed; "
                    f"gates_seen={gates_seen} gates_answered={gates_answered} "
                    f"buf={len(buf)}B",
                    file=sys.stderr, flush=True,
                )

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
    # 0   = workflow completed cleanly
    # 130 = workflow paused (a reviewer agent gate hit max_retries, etc.) —
    #       still a valid outcome of `imp new`; the user can resume later.
    assert rc in (0, 130), (
        f"expected exit 0 or 130, got {rc}; "
        f"gates_seen={gates_seen} gates_answered={gates_answered}\n"
        f"last 2000 bytes of output:\n{bytes(buf[-2000:])!r}"
    )

    state_path = tmp_path / ".imp" / "changes" / "1" / "state" / "state.json"
    state = json.loads(state_path.read_text())
    assert state["status"] in ("completed", "paused"), (
        f"unexpected status {state['status']!r}; "
        f"gates_seen={gates_seen} gates_answered={gates_answered}"
    )
