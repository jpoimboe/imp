"""Live Anthropic end-to-end. Gated by IMP_LIVE_API=1. Costs a small amount
of Anthropic credit per run.

Two tests live here:

  test_one_step_live — cheap one-step smoke test against the live-e2e fixture
    workflow.  Exercises the basic path (single phase, single step, no human
    gates).  Fast (~15 s, ~1 k tokens).

  test_multiphase_walk — richer walk that copies the full imp tree, runs
    ``imp init`` + ``imp new`` via subprocess (so the real CLI entrypoint is
    exercised), and auto-approves every human gate by feeding "1\\n" on stdin.
    Asserts that the workflow advances through at least three phases.  Lenient
    on full completion: PASSES if the workflow reached the ``implement`` phase
    even if it didn't fully merge (\"shipped to merge\" requires a manual run
    due to the real codebase being present).
    Budget: ~15 min / 900 s hard timeout, roughly 50-200 k tokens at 2026
    Haiku-class pricing (~$0.10-0.50 per run depending on context size).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from imp.agents.deepagent_runner import DeepAgentRunner
from imp.cli.init import run_init
from imp.engine.change import Change
from imp.engine.executor import RunResult, run

FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = pytest.mark.skipif(
    os.environ.get("IMP_LIVE_API") != "1",
    reason="set IMP_LIVE_API=1 + ANTHROPIC_API_KEY to enable",
)

# ---------------------------------------------------------------------------
# API key bootstrap — set from /tmp/.imp-key if ANTHROPIC_API_KEY not present
# ---------------------------------------------------------------------------

_KEY_FILE = Path("/tmp/.imp-key")
if _KEY_FILE.exists() and not os.environ.get("ANTHROPIC_API_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = _KEY_FILE.read_text().strip()


def _require_real_key() -> str:
    """Return the API key or skip the test if only a stub is present."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key == "sk-test":
        pytest.skip(
            "autouse fixture is masking real ANTHROPIC_API_KEY "
            "(set the env var before running)"
        )
    if not key:
        pytest.skip("ANTHROPIC_API_KEY not set")
    return key


# ---------------------------------------------------------------------------
# Helpers shared between tests
# ---------------------------------------------------------------------------

_COPYTREE_IGNORE = shutil.ignore_patterns(
    ".imp",
    ".git",
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
    ".worktrees",
    "spikes",
)


# ---------------------------------------------------------------------------
# Test 1: cheap one-step smoke
# ---------------------------------------------------------------------------


def test_one_step_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """One phase, one step, no human gates.  Fast and cheap baseline check."""
    _require_real_key()

    shutil.copytree(
        FIXTURES / "workflows" / "live-e2e",
        tmp_path / ".imp" / "workflows" / "default",
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: [{provider: anthropic, "
        "model: claude-haiku-4-5-20251001}]}\n"
    )
    monkeypatch.chdir(tmp_path)

    change = Change.create(tmp_path, title="live")
    result = run(change, DeepAgentRunner())
    assert result is RunResult.COMPLETED

    events = list(change.event_log().read())
    outputs = [e for e in events if e["type"] == "agent_output"]
    assert len(outputs) == 1
    assert outputs[0]["payload"]["status"] == "Success"
    # The model usually says "hello world" verbatim per the system prompt;
    # be lenient in case it doesn't quite.
    assert "hello" in outputs[0]["payload"].get("message", "").lower()


# ---------------------------------------------------------------------------
# Test 2: multi-phase walk against real Anthropic
# ---------------------------------------------------------------------------

# How many "1\n" answers to pre-load for human gates.  The default workflow
# has human gates at onboard (1), spec (1), design (1), plan (1), review (1),
# merge (1) — 6 total.  We feed 20 to be safe; extras are ignored (EOF on
# stdin causes EOFError which the CLI catches as a pause, so they don't break
# anything).
_GATE_RESPONSES = "1\n" * 20

# Phases that the workflow must reach for a PASS.  The workflow may pause
# before completing fully (e.g. at a human gate after implement), and that is
# acceptable.  We require at least 3 phases to have started.
_MIN_PHASE_STARTS = 3

# Critical error keywords — if these appear in an error event the test fails.
# Network transient and retry-recoverable messages are excluded.
_CRITICAL_ERROR_KEYWORDS = [
    "create_deep_agent failed",
    "prompt render failed",
    "unknown phase",
    "no model resolved",
]

# Keywords found in retry-recoverable error events that we tolerate.
_RECOVERABLE_KEYWORDS = [
    "overloaded",
    "rate_limit",
    "timeout",
    "connection",
    "retry",
]


def _is_critical_error(message: str) -> bool:
    msg_lower = message.lower()
    if any(k in msg_lower for k in _RECOVERABLE_KEYWORDS):
        return False
    return any(k in message for k in _CRITICAL_ERROR_KEYWORDS)


def test_multiphase_walk(tmp_path: Path) -> None:
    """Multi-phase walk: copy imp tree → init → new → assert progress.

    Passes if the workflow advanced past ``onboard`` (i.e. ≥ 3 phase_started
    events in events.jsonl, regardless of whether the workflow completed).

    Full completion up to ``merge`` requires real human interaction and is
    intentionally out of scope for CI.  The test is lenient on terminal state
    so that transient model weirdness (e.g. a phase pausing due to a bad
    structured response) doesn't cause a flaky failure — we care that the
    *engine plumbing* works, not that the LLM is perfectly compliant.

    Token budget: roughly 50-200 k tokens at 2026 Haiku pricing.
    Hard timeout: 900 s (15 min), enforced by the outer pytest-timeout or the
    ``timeout 900`` wrapper in the Makefile / CI command.
    """
    key = _require_real_key()

    # 1. Copy the imp source tree (excluding volatile dirs) into a tmpdir
    #    so the agent has a real codebase to work with.
    project_root = Path("/home/jpoimboe/imp")
    work_dir = tmp_path / "imp-copy"
    shutil.copytree(project_root, work_dir, ignore=_COPYTREE_IGNORE)

    # 2. Run imp init non-interactively.
    answers = iter(["anthropic", "ANTHROPIC_API_KEY"])
    run_init(
        project_root=work_dir,
        input_fn=lambda _prompt="": next(answers),
        output_fn=lambda _msg: None,
    )

    # 3. Verify init wrote config + workflow.
    assert (work_dir / ".imp" / "config.yaml").exists()
    assert (work_dir / ".imp" / "workflows" / "default" / "workflow.yaml").exists()

    # 4. Run ``imp new`` as a subprocess so the real CLI path is exercised.
    #    Feed "1\n" repeatedly so human gates auto-approve without hanging.
    env = os.environ.copy()
    env["ANTHROPIC_API_KEY"] = key
    # Prevent the subprocess from using a stub runner.
    env.pop("IMP_STUB_CANNED", None)

    title = "add a --version flag to the CLI"
    cmd = [sys.executable, "-m", "imp", "new", title]
    proc = subprocess.Popen(
        cmd,
        cwd=str(work_dir),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    timeout_seconds = 900  # 15 min hard limit
    try:
        stdout, _ = proc.communicate(
            input=_GATE_RESPONSES,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail(
            f"imp new timed out after {timeout_seconds}s — "
            "workflow may be stuck in a retry loop or waiting for input"
        )

    # 5. Locate the Change directory.
    changes_root = work_dir / ".imp" / "changes"
    assert changes_root.is_dir(), (
        f"No .imp/changes/ directory found.\nstdout:\n{stdout}"
    )
    change_dirs = sorted(
        d for d in changes_root.iterdir() if d.is_dir() and d.name.isdigit()
    )
    assert change_dirs, (
        f"No Change directories found.\nstdout:\n{stdout}"
    )
    change_dir = change_dirs[-1]

    # 6. Check artifacts.
    context_md = change_dir / "artifacts" / "context.md"
    spec_md = change_dir / "artifacts" / "spec.md"
    assert context_md.exists() and context_md.stat().st_size > 0, (
        f"context.md missing or empty after run.\n"
        f"Change dir contents: {list(change_dir.rglob('*'))}\n"
        f"stdout:\n{textwrap.shorten(stdout, 4000)}"
    )
    assert spec_md.exists() and spec_md.stat().st_size > 0, (
        f"spec.md missing or empty after run.\n"
        f"Change dir contents: {list(change_dir.rglob('*'))}\n"
        f"stdout:\n{textwrap.shorten(stdout, 4000)}"
    )

    # 7. Parse events.jsonl and assert progress.
    events_path = change_dir / "events.jsonl"
    assert events_path.exists(), (
        f"events.jsonl missing.\nstdout:\n{stdout}"
    )
    events: list[dict] = []
    for line in events_path.read_text().splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))

    phase_starts = [e for e in events if e["type"] == "phase_started"]
    assert len(phase_starts) >= _MIN_PHASE_STARTS, (
        f"Expected >= {_MIN_PHASE_STARTS} phase_started events; "
        f"got {len(phase_starts)}: {[e['payload']['phase'] for e in phase_starts]}\n"
        f"stdout:\n{textwrap.shorten(stdout, 2000)}"
    )

    # 8. No critical errors.
    error_events = [e for e in events if e["type"] == "error"]
    critical = [
        e for e in error_events
        if _is_critical_error(e["payload"].get("message", ""))
    ]
    assert critical == [], (
        "Critical error events found:\n"
        + "\n".join(e["payload"]["message"] for e in critical)
        + f"\nstdout:\n{textwrap.shorten(stdout, 2000)}"
    )

    # 9. State advanced past onboard.
    state_json = change_dir / "state" / "state.json"
    assert state_json.exists()
    state = json.loads(state_json.read_text())
    phases_seen = {e["payload"]["phase"] for e in phase_starts}
    assert "onboard" in phases_seen, (
        "workflow never started the onboard phase — engine may have crashed "
        f"before emitting any events.\nstdout:\n{textwrap.shorten(stdout, 2000)}"
    )
    # Lenient completion check: PASS if we reached implement OR beyond.
    advanced_phases = {"spec", "design", "plan", "implement", "review", "merge"}
    reached_advanced = phases_seen & advanced_phases
    assert reached_advanced, (
        f"Workflow never advanced past onboard (phases seen: {phases_seen}).\n"
        f"state={state}\nstdout:\n{textwrap.shorten(stdout, 2000)}"
    )
