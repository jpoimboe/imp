"""PTY-driven shipped default workflow end-to-end against a real LLM.

This is the integration test that proves:
  1. The shipped workflow tree (`workflow.yaml`, `scripts/`, agents) is
     byte-identical to what real users run — no test-only edits.
  2. The driver decides when to act based on engine-emitted events
     (`human_gate_pending`, `tool_call` with name=ask_user) rather than
     matching renderer output strings.
  3. Every yaml-declared human gate gets answered exactly once.
  4. `imp log` and `imp log --full` smoke-pass against the resulting
     events.jsonl.

Skip predicates (all must pass):
  - first_opted_in() is not None  (IMP_LIVE_PROVIDERS opt-in)
  - IMP_RUN_LIVE_TESTS=1          (cost guard; set by `make test-live`)
  - _can_pty()                    (Windows / sandboxed CI without /dev/pty*)
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests._live import first_opted_in

# ── Helpers ────────────────────────────────────────────────────────────────


def _anthropic_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    key_file = Path.home() / ".anthropic-key"
    if key_file.exists():
        return key_file.read_text().strip()
    return None


def _openai_key() -> str | None:
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    key_file = Path.home() / ".openai-key"
    if key_file.exists():
        return key_file.read_text().strip()
    return None


def _provider_model(provider: str) -> str:
    return {
        "anthropic": "claude-sonnet-4-6",
        "openai": "gpt-4o-mini",
        "claude_code": "claude-sonnet-4-5",
    }[provider]


def _can_pty() -> bool:
    try:
        import pty
        m, s = pty.openpty()
        os.close(m)
        os.close(s)
        return True
    except Exception:
        return False


def _walk_yaml_human_gates(wf_yaml: Path) -> list[tuple[str, int]]:
    """Return [(phase_name, gate_index), ...] for every human gate declared
    in `workflow.yaml`. The driver uses len(...) to know how many human
    gates should fire end-to-end."""
    data = yaml.safe_load(wf_yaml.read_text())
    out: list[tuple[str, int]] = []
    for phase in data.get("phases", []):
        gates = (data.get(phase) or {}).get("gates") or []
        for idx, g in enumerate(gates):
            if g.get("type") == "human":
                out.append((phase, idx))
    return out


def _walk_yaml_interactive_steps(wf_yaml: Path) -> set[str]:
    """Step names with `interactive: true`. Used for informational
    assertions / diagnostics — not load-bearing."""
    data = yaml.safe_load(wf_yaml.read_text())
    out: set[str] = set()
    for phase in data.get("phases", []):
        for step in (data.get(phase) or {}).get("steps", []) or []:
            if step.get("interactive") is True:
                out.add(step["name"])
    return out


# ── Skip predicates ────────────────────────────────────────────────────────

pytestmark = [
    pytest.mark.skipif(
        first_opted_in() is None,
        reason="no provider in IMP_LIVE_PROVIDERS (PTY test uses the first)",
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


def test_full_default_workflow_via_pty_events_driven(tmp_path: Path) -> None:
    """Drive the shipped default workflow start to finish using events.jsonl.

    The shipped tree is copied byte-identical (no edits to workflow.yaml,
    no edits to scripts/). The PTY driver subscribes to events.jsonl and
    answers `human_gate_pending` with "1\\n" and `tool_call(ask_user)` with
    a canned "looks good" reply. PTY output is drained for kernel-buffer
    hygiene but never pattern-matched.
    """
    import pty

    provider = first_opted_in()
    assert provider is not None  # guaranteed by skipif
    model = _provider_model(provider)

    # Auth assertion (opt-in implies user has set up credentials).
    if provider == "anthropic":
        assert _anthropic_key() is not None, (
            "anthropic opted in but no ANTHROPIC_API_KEY / ~/.anthropic-key"
        )
    elif provider == "openai":
        assert _openai_key() is not None, (
            "openai opted in but no OPENAI_API_KEY / ~/.openai-key"
        )
    elif provider == "claude_code":
        assert shutil.which("claude") is not None, (
            "claude_code opted in but `claude` CLI not on PATH"
        )

    # Seed the shipped workflow tree — BYTE-IDENTICAL, no edits.
    src = (
        Path(__file__).resolve().parents[1]
        / "src" / "imp" / "assets" / "workflows" / "default"
    )
    dst = tmp_path / ".imp" / "workflows" / "default"
    shutil.copytree(src, dst)

    wf_yaml = dst / "workflow.yaml"
    expected_gates = _walk_yaml_human_gates(wf_yaml)
    interactive_steps = _walk_yaml_interactive_steps(wf_yaml)
    assert expected_gates, "no human gates in workflow.yaml — test would never act"

    # Config: every model_role → picked provider/model.
    role_line = "{prefer: [{provider: " + provider + ", model: " + model + "}]}"
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n"
        f"  default:     {role_line}\n"
        f"  spec:        {role_line}\n"
        f"  spec_review: {role_line}\n"
        f"  code:        {role_line}\n"
        f"  code_review: {role_line}\n"
    )

    env = dict(os.environ)
    if provider == "anthropic":
        env["ANTHROPIC_API_KEY"] = _anthropic_key()  # type: ignore[assignment]
    elif provider == "openai":
        env["OPENAI_API_KEY"] = _openai_key()  # type: ignore[assignment]
    # claude_code uses the user's subscription via the CLI; no env injection.
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

    # start_new_session=True puts the subprocess in its own PG so Ctrl-C from
    # the user's terminal does NOT reach it directly. Worse: raising
    # KeyboardInterrupt from a signal handler doesn't reliably abort a pytest
    # test (some plugins / capture modes swallow it). We hard-kill the
    # subprocess PG and then os._exit so Ctrl-C is unambiguous.
    # os.write(2, ...) bypasses pytest's sys.stderr capture so the message
    # surfaces even when -v is buffering output.
    def _on_sigint(_signum: int, _frame: Any) -> None:
        with contextlib.suppress(OSError):
            os.write(2, b"\n[pty-test] SIGINT received; killing subprocess and aborting\n")
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        os._exit(130)
    _prev_sigint = signal.signal(signal.SIGINT, _on_sigint)

    events_file = tmp_path / ".imp" / "changes" / "1" / "events.jsonl"

    # Per spec: 720s wall-clock cap accommodates max_retries: 3 left intact.
    TOTAL_TIMEOUT = 720.0
    PROGRESS_INTERVAL = 5.0
    EVENTS_FIRST_APPEAR_TIMEOUT = 10.0  # fail-fast if engine never starts
    TRACE_PTY = os.environ.get("IMP_PTY_TRACE") == "1"

    buf = bytearray()  # PTY output buffer for diagnostics only
    gates_answered = 0
    asks_answered = 0
    events_seen_bytes = 0  # byte offset in events.jsonl already processed
    ask_questions: list[str] = []  # diagnostic
    start_time = time.time()
    deadline = start_time + TOTAL_TIMEOUT
    last_progress = start_time
    events_first_seen_at: float | None = None

    print(
        f"\n[pty-test] provider={provider} model={model} starting; "
        f"expected_gates={len(expected_gates)} "
        f"interactive_steps={interactive_steps}",
        file=sys.stderr, flush=True,
    )

    def _drain_pty() -> None:
        nonlocal buf
        ready, _, _ = select.select([master], [], [], 0.05)
        if ready:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                return
            if chunk:
                buf.extend(chunk)
                if TRACE_PTY:
                    sys.stderr.buffer.write(chunk)
                    sys.stderr.buffer.flush()

    def _tail_events() -> list[dict[str, Any]]:
        """Read new lines from events.jsonl past events_seen_bytes.

        Returns the list of newly parsed events (skipping any partial-write
        residue at the tail; that line is re-read next tick)."""
        nonlocal events_seen_bytes, events_first_seen_at
        if not events_file.exists():
            return []
        if events_first_seen_at is None:
            events_first_seen_at = time.time()
        try:
            with events_file.open("rb") as f:
                f.seek(events_seen_bytes)
                raw = f.read()
        except OSError:
            return []
        if not raw:
            return []
        # If raw doesn't end with newline, the last line is partial — defer.
        text = raw.decode("utf-8", errors="replace")
        if not text.endswith("\n"):
            # Find the last newline; advance events_seen_bytes to just past it.
            last_nl = text.rfind("\n")
            if last_nl < 0:
                return []  # no complete line yet
            complete = text[: last_nl + 1]
            events_seen_bytes += len(complete.encode("utf-8"))
            text = complete
        else:
            events_seen_bytes += len(raw)

        out: list[dict[str, Any]] = []
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    try:
        while time.time() < deadline:
            _drain_pty()

            # Fail-fast if events.jsonl never appears.
            if (
                not events_file.exists()
                and time.time() - start_time > EVENTS_FIRST_APPEAR_TIMEOUT
            ):
                proc.kill()
                proc.wait()
                pytest.fail(
                    f"events.jsonl did not appear within "
                    f"{EVENTS_FIRST_APPEAR_TIMEOUT}s of process start; "
                    f"engine did not initialize.\n"
                    f"last 2000 bytes of PTY output:\n{bytes(buf[-2000:])!r}"
                )

            for ev in _tail_events():
                etype = ev.get("type")
                payload = ev.get("payload") or {}
                if etype == "human_gate_pending":
                    phase = payload.get("phase", "?")
                    # Sanity: phase should appear in expected_gates.
                    if not any(p == phase for p, _ in expected_gates):
                        proc.kill()
                        proc.wait()
                        pytest.fail(
                            f"engine emitted human_gate_pending for phase "
                            f"{phase!r} not in yaml-declared human gates "
                            f"{expected_gates}; workflow.yaml/parse drift?"
                        )
                    os.write(master, b"1\n")
                    gates_answered += 1
                    print(
                        f"[pty-test] answered human gate #{gates_answered} "
                        f"(phase={phase})",
                        file=sys.stderr, flush=True,
                    )
                elif (
                    etype == "tool_call"
                    and payload.get("name") == "ask_user"
                ):
                    args = payload.get("args") or {}
                    question = args.get("question") if isinstance(args, dict) else None
                    if isinstance(question, str):
                        ask_questions.append(question)
                    os.write(
                        master,
                        b"looks good, proceed with reasonable defaults\n",
                    )
                    asks_answered += 1
                    print(
                        f"[pty-test] answered ask_user #{asks_answered} "
                        f"question={question!r}",
                        file=sys.stderr, flush=True,
                    )
                elif etype == "tool_result":
                    # Fail fast on the known-bad signature of an unwired
                    # interactive step. Otherwise the agent retries other
                    # tools and the bug only surfaces at the 720s timeout
                    # via the asks_answered>=1 final assertion — very slow
                    # to diagnose.
                    result_text = json.dumps(payload.get("result"))
                    if "ask_user not available in non-interactive step" in result_text:
                        proc.kill()
                        proc.wait()
                        pytest.fail(
                            "ask_user returned 'not available in non-interactive "
                            "step' — interactive flag is not flowing from the "
                            "workflow.yaml step to the provider runner. Check "
                            "AgentInvocation.user_input_fn wiring in executor + "
                            "providers/*.create_runner."
                        )

            now = time.time()
            if now - last_progress >= PROGRESS_INTERVAL:
                last_progress = now
                print(
                    f"[pty-test] {now - start_time:.0f}s elapsed; "
                    f"gates_answered={gates_answered}/{len(expected_gates)} "
                    f"asks_answered={asks_answered} "
                    f"events_bytes={events_seen_bytes} "
                    f"pty_buf={len(buf)}B",
                    file=sys.stderr, flush=True,
                )

            if proc.poll() is not None:
                # Drain whatever's left in the PTY for diagnostics.
                for _ in range(20):
                    _drain_pty()
                break

            time.sleep(0.05)
        else:
            # Wall-clock cap hit.
            proc.kill()
            proc.wait()
            pytest.fail(
                f"PTY workflow exceeded {TOTAL_TIMEOUT}s wall clock "
                f"(may be reviewer flake; rerun once before investigating).\n"
                f"gates_answered={gates_answered}/{len(expected_gates)} "
                f"asks_answered={asks_answered}\n"
                f"last 2000 bytes of PTY output:\n{bytes(buf[-2000:])!r}"
            )
    finally:
        signal.signal(signal.SIGINT, _prev_sigint)
        with contextlib.suppress(OSError):
            os.close(master)
        if proc.poll() is None:
            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.kill()
            proc.wait()

    rc = proc.wait(timeout=10)
    assert rc == 0, (
        f"expected exit 0, got {rc}; "
        f"gates_answered={gates_answered}/{len(expected_gates)} "
        f"asks_answered={asks_answered}\n"
        f"last 2000 bytes of PTY output:\n{bytes(buf[-2000:])!r}"
    )

    state_path = tmp_path / ".imp" / "changes" / "1" / "state" / "state.json"
    state = json.loads(state_path.read_text())
    assert state["status"] == "completed", (
        f"expected status=completed, got {state['status']!r}"
    )

    assert gates_answered == len(expected_gates), (
        f"expected {len(expected_gates)} yaml-declared human gates to fire, "
        f"saw {gates_answered}"
    )
    assert asks_answered >= 1, (
        "interactive spec phase should produce at least one ask_user call; "
        "saw 0"
    )

    # ── imp log smoke (still inside this test) ─────────────────────────

    log_out = subprocess.run(
        [sys.executable, "-m", "imp", "log", "1"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert log_out.returncode == 0, (
        f"imp log rc={log_out.returncode}\nstderr: {log_out.stderr!r}"
    )
    assert log_out.stdout.strip(), "imp log produced empty stdout"

    log_full = subprocess.run(
        [sys.executable, "-m", "imp", "log", "1", "--full"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert log_full.returncode == 0, (
        f"imp log --full rc={log_full.returncode}\nstderr: {log_full.stderr!r}"
    )
    assert log_full.stdout.strip(), "imp log --full produced empty stdout"

    # The implement phase always emits tool_call events (coder uses
    # write_file/edit_file/bash). Default mode collapses bursts into a
    # synthetic "tools" line; full mode keeps individual tool_call lines.
    assert "tools" in log_out.stdout
    assert "tool_call" in log_full.stdout
    # The synthetic "tools" event type (left-padded inside the type column
    # with depth-4 indent) should NOT appear in full mode.
    assert "    tools  " not in log_full.stdout
