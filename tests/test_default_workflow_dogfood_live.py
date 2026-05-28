"""Dogfood end-to-end test: imp on imp's own source.

This is the integration test that proves the shipped default workflow runs
end-to-end against a real LLM on a real (non-trivial) project — imp's own
source tree. The coder agent makes a small, deterministic edit (a sentinel
print at the top of the bash tool's function); we verify the workflow ran
to completion, all events fired correctly, and the sentinel landed in the
expected file.

What this proves:
  1. The shipped workflow tree (`workflow.yaml`, `scripts/`, agents) runs
     against real code, not an empty tmp_path.
  2. `scripts/run-tests.sh` actually exercises a real `make test`.
  3. The reviewer reviews a real diff.
  4. The driver decides when to act based on engine-emitted events,
     not by matching renderer output strings.
  5. Every yaml-declared human gate gets answered exactly once.
  6. `imp log` and `imp log --full` smoke-pass.
  7. The coder's specific code change landed (sentinel grep).

Skip predicates (all must pass):
  - first_opted_in() is not None  (IMP_LIVE_PROVIDERS opt-in)
  - IMP_RUN_DOGFOOD=1             (cost guard; set by `make test-dogfood`)
  - _can_pty()                    (Windows / sandboxed CI without /dev/pty*)

Recursion guard: the coder runs `make test` -> `pytest -q` in the copied
tree, which would re-collect this file. Scrubbing IMP_LIVE_PROVIDERS from
the subprocess env causes all live tests to skip via opted_in().
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
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests._live import first_opted_in

# ── Constants ──────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
    in workflow.yaml. The driver uses len(...) to know how many human gates
    should fire end-to-end."""
    data = yaml.safe_load(wf_yaml.read_text())
    out: list[tuple[str, int]] = []
    for phase in data.get("phases", []):
        gates = (data.get(phase) or {}).get("gates") or []
        for idx, g in enumerate(gates):
            if g.get("type") == "human":
                out.append((phase, idx))
    return out


def _ignore_for_copy(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names
            if n in {".venv", ".git", ".imp", ".pytest_cache",
                     ".mypy_cache", ".ruff_cache", ".coverage",
                     "htmlcov", ".tox", "__pycache__", "dist", "build"}
            or n.endswith(".egg-info")}


def _seed_dogfood_project(tmp_path: Path, provider: str) -> None:
    """Copy imp source into tmp_path, init git, bootstrap .imp/ via run_init."""
    shutil.copytree(PROJECT_ROOT, tmp_path, ignore=_ignore_for_copy,
                    dirs_exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git",
         "-c", "user.email=test@imp",
         "-c", "user.name=imp-dogfood",
         "commit", "-q", "-m", "initial"],
        cwd=tmp_path, check=True,
    )

    # Bootstrap .imp/ via the real init code path with injected I/O.
    # (`imp init` has no flags — it always launches a TUI picker and
    # prompts for env-var names. run_init accepts callbacks so callers
    # can drive it without a TTY.)
    from imp.cli.init import run_init
    run_init(
        project_root=tmp_path,
        input_fn=lambda _prompt: "",
        output_fn=lambda _line: None,
        picker_fn=lambda _available: [provider],
    )


# ── Skip predicates ────────────────────────────────────────────────────────

pytestmark = [
    pytest.mark.skipif(
        first_opted_in() is None,
        reason="no provider in IMP_LIVE_PROVIDERS",
    ),
    pytest.mark.skipif(
        os.environ.get("IMP_RUN_DOGFOOD") != "1",
        reason="dogfood run on real LLM; set IMP_RUN_DOGFOOD=1 to opt in",
    ),
    pytest.mark.skipif(
        not _can_pty(),
        reason="pty not available (Windows or sandboxed CI)",
    ),
]


# ── Test ───────────────────────────────────────────────────────────────────


def test_dogfood_imp_on_imp_default_workflow(tmp_path: Path) -> None:
    """Run the shipped default workflow against imp's own source.

    Dogfoods imp by asking the workflow to add a unique sentinel print to
    src/imp/agents/tools/bash.py. After completion, verifies the sentinel
    landed in the right file plus all the events.jsonl/imp-log invariants
    the prior PTY test had.
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

    # Seed the project: copy imp source, git init, run_init.
    _seed_dogfood_project(tmp_path, provider)

    wf_yaml = tmp_path / ".imp" / "workflows" / "default" / "workflow.yaml"
    expected_gates = _walk_yaml_human_gates(wf_yaml)
    assert expected_gates, "no human gates in workflow.yaml — test would never act"

    # Override config.yaml to pin the exact model string. run_init wrote
    # provider-default models; we want determinism across run_init updates.
    role_line = "{prefer: [{provider: " + provider + ", model: " + model + "}]}"
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n"
        f"  default:     {role_line}\n"
        f"  spec:        {role_line}\n"
        f"  spec_review: {role_line}\n"
        f"  code:        {role_line}\n"
        f"  code_review: {role_line}\n"
    )

    # Title carries the feature intent. Unique sentinel per run prevents
    # stale state from faking a pass.
    sentinel = f"IMP_DOGFOOD_{uuid.uuid4().hex[:12]}"
    title = (
        f"Edit src/imp/agents/tools/bash.py and add exactly this line as "
        f"the first statement inside the existing function decorated with "
        f'@imp_tool(name="bash", ...): '
        f"print({sentinel!r}, file=sys.stderr). Add `import sys` at the "
        f"top of the file if not already imported. Do not refactor the "
        f"function, do not move the print into a helper, do not change "
        f"anything else."
    )

    env = dict(os.environ)
    # Recursion guard: the coder's `make test` re-collects this file;
    # scrubbing IMP_LIVE_PROVIDERS makes all live tests skip in the
    # subprocess pytest run.
    env.pop("IMP_LIVE_PROVIDERS", None)
    env.pop("IMP_RUN_LIVE_TESTS", None)
    # NOTE: keep this list in sync with any future opt-in/cost-guard env
    # vars added to tests/. Named list (not prefix sweep) is intentional:
    # explicit > clever, and a misnamed future guard wouldn't be caught
    # either way.
    if provider == "anthropic":
        env["ANTHROPIC_API_KEY"] = _anthropic_key()  # type: ignore[assignment]
    elif provider == "openai":
        env["OPENAI_API_KEY"] = _openai_key()  # type: ignore[assignment]
    # claude_code: requires only `claude` on PATH; CLI handles auth via
    # the user's local subscription (keyring). No env injection.

    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-m", "imp", "new", title],
        cwd=tmp_path,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        start_new_session=True,
    )
    os.close(slave)

    # SIGINT handling: hard-kill the subprocess PG so Ctrl-C is
    # unambiguous (raising KeyboardInterrupt from a signal handler is
    # unreliable inside pytest — some plugins swallow it).
    def _on_sigint(_signum: int, _frame: Any) -> None:
        with contextlib.suppress(OSError):
            os.write(2, b"\n[dogfood] SIGINT received; killing subprocess and aborting\n")
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        os._exit(130)
    _prev_sigint = signal.signal(signal.SIGINT, _on_sigint)

    events_file = tmp_path / ".imp" / "changes" / "1" / "events.jsonl"

    TOTAL_TIMEOUT = 1800.0
    PROGRESS_INTERVAL = 10.0
    EVENTS_FIRST_APPEAR_TIMEOUT = 30.0
    TRACE_PTY = os.environ.get("IMP_PTY_TRACE") == "1"

    buf = bytearray()
    gates_answered = 0
    asks_answered = 0
    events_seen_bytes = 0
    ask_questions: list[str] = []
    start_time = time.time()
    deadline = start_time + TOTAL_TIMEOUT
    last_progress = start_time
    events_first_seen_at: float | None = None

    print(
        f"\n[dogfood] provider={provider} model={model} starting; "
        f"sentinel={sentinel} expected_gates={len(expected_gates)}",
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
        text = raw.decode("utf-8", errors="replace")
        if not text.endswith("\n"):
            last_nl = text.rfind("\n")
            if last_nl < 0:
                return []
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
                        f"[dogfood] answered human gate #{gates_answered} "
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
                        f"[dogfood] answered ask_user #{asks_answered} "
                        f"question={question!r}",
                        file=sys.stderr, flush=True,
                    )
                elif etype == "workflow_paused":
                    # Pause fail-fast: the engine emits workflow_paused at
                    # executor.py:100/106 whenever it halts (including
                    # on_max_retries: pause). The driver has no resumer,
                    # so without this branch we'd just burn the wall-clock.
                    reason = payload.get("reason", "(no reason recorded)")
                    proc.kill()
                    proc.wait()
                    pytest.fail(
                        f"engine emitted workflow_paused (reason={reason!r}); "
                        f"likely max_retries exhausted in a step or phase. "
                        f"gates_answered={gates_answered}/{len(expected_gates)} "
                        f"asks_answered={asks_answered}\n"
                        f"last 2000 bytes of PTY output:\n{bytes(buf[-2000:])!r}"
                    )

            now = time.time()
            if now - last_progress >= PROGRESS_INTERVAL:
                last_progress = now
                print(
                    f"[dogfood] {now - start_time:.0f}s elapsed; "
                    f"gates_answered={gates_answered}/{len(expected_gates)} "
                    f"asks_answered={asks_answered} "
                    f"events_bytes={events_seen_bytes} "
                    f"pty_buf={len(buf)}B",
                    file=sys.stderr, flush=True,
                )

            if proc.poll() is not None:
                for _ in range(20):
                    _drain_pty()
                break

            time.sleep(0.05)
        else:
            proc.kill()
            proc.wait()
            pytest.fail(
                f"dogfood workflow exceeded {TOTAL_TIMEOUT}s wall clock "
                f"(may be reviewer flake or coder iteration storm; "
                f"rerun once before investigating).\n"
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

    # ── Dogfood-specific verification ──────────────────────────────────

    # Sentinel landed in the target file.
    bash_py = tmp_path / "src" / "imp" / "agents" / "tools" / "bash.py"
    assert bash_py.exists(), (
        f"{bash_py} missing after dogfood run — coder deleted the target file?"
    )
    bash_src = bash_py.read_text()
    assert sentinel in bash_src, (
        f"sentinel {sentinel!r} not found in {bash_py}; coder either skipped "
        f"the edit, placed the sentinel elsewhere, or the documenter "
        f"clobbered it.\nLast 500 chars of bash.py:\n{bash_src[-500:]}"
    )

    # At least one commit beyond the initial seed commit.
    log_oneline = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=tmp_path, capture_output=True, text=True, timeout=10, check=True,
    )
    commit_count = len([
        ln for ln in log_oneline.stdout.splitlines() if ln.strip()
    ])
    assert commit_count >= 2, (
        f"expected >=2 commits (initial + coder's), got {commit_count}:\n"
        f"{log_oneline.stdout}"
    )

    # Artifacts present.
    artifacts_dir = tmp_path / ".imp" / "changes" / "1" / "artifacts"
    for name in ("spec.md", "plan.md"):
        path = artifacts_dir / name
        assert path.exists(), f"expected artifact {path} after dogfood run"

    # ── imp log smoke ──────────────────────────────────────────────────

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

    assert "tools" in log_out.stdout
    assert "tool_call" in log_full.stdout
    assert "    tools  " not in log_full.stdout
