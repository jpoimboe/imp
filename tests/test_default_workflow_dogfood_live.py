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
import fcntl
import json
import os
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
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


def _read_all_events(events_file: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not events_file.exists():
        return out
    for line in events_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        with contextlib.suppress(json.JSONDecodeError):
            out.append(json.loads(line))
    return out


def _walk_yaml_human_gates(wf_yaml: Path) -> list[tuple[str, str]]:
    """Return [(phase_name, step_name), ...] for every human step (carrying
    `ask:`/`options:`) declared in the unified-surface workflow.yaml. The driver
    uses len(...) to know how many human prompts should fire end-to-end."""
    data = yaml.safe_load(wf_yaml.read_text())
    out: list[tuple[str, str]] = []
    for phase in data.get("phases", []):
        if not isinstance(phase, dict):
            continue
        pname = str(phase.get("name", ""))
        for step in phase.get("steps") or []:
            if isinstance(step, dict) and ("ask" in step or "options" in step):
                out.append((pname, str(step.get("name", ""))))
    return out


def _ignore_for_copy(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names
            if n in {".venv", ".git", ".imp", ".pytest_cache",
                     ".mypy_cache", ".ruff_cache", ".coverage",
                     "htmlcov", ".tox", "__pycache__", "dist", "build"}
            or n.endswith(".egg-info")}


def _seed_dogfood_project(tmp_path: Path, provider: str) -> Path:
    """Copy imp source into tmp_path, init git, bootstrap .imp/, and create an
    ISOLATED venv whose `imp` is the temp copy (not the editable host repo).

    Returns the venv's python interpreter path. Callers MUST spawn the engine
    (and anything that re-imports imp) under this interpreter, and must prepend
    its bin/ to PATH so the copy's `make test` resolves python3 to it.
    """
    shutil.copytree(PROJECT_ROOT, tmp_path, ignore=_ignore_for_copy,
                    dirs_exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    # Bootstrap .imp/ via the real init code path with injected I/O — BEFORE the
    # initial commit. run_init both (a) appends `.imp/` to the project
    # .gitignore and (b) writes .imp/ working state. Running it pre-commit means
    # the single seed commit captures the gitignore append while `.imp/`
    # (wholesale-ignored by Fix D) contributes NOTHING to history — so the
    # post-run "clean working tree" and "no .imp/ in history" hygiene gates
    # (Task 14) hold on a healthy run, and commit_count stays 2 (seed + coder).
    from imp.cli.init import run_init
    run_init(
        project_root=tmp_path,
        input_fn=lambda _prompt: "",
        output_fn=lambda _line: None,
        picker_fn=lambda _available: [provider],
    )

    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git",
         "-c", "user.email=test@imp",
         "-c", "user.name=imp-dogfood",
         "commit", "-q", "-m", "initial"],
        cwd=tmp_path, check=True,
    )
    # Sanity: with Fix D's wholesale `.imp/` ignore, the seed commit must not
    # contain any .imp/ path (the no-.imp-in-history gate depends on this).
    tracked_imp = subprocess.run(
        ["git", "ls-files", ".imp/"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    assert not tracked_imp.stdout.strip(), (
        f"seed commit tracked .imp/ paths (Fix D gitignore wrong?):\n"
        f"{tracked_imp.stdout}"
    )

    # Isolated venv: --system-site-packages reuses host deps (fast); editable
    # install of the COPY (--no-deps) puts the copy's src/ ahead of the host
    # editable path-entry on THIS venv's sys.path, so only `imp` is overridden.
    venv_dir = tmp_path / ".venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(venv_dir)],
        check=True,
    )
    venv_python = venv_dir / "bin" / "python"
    subprocess.run(
        [str(venv_python), "-m", "pip", "install", "-e", str(tmp_path),
         "--no-deps", "-q"],
        check=True,
    )

    # Pre-flight: prove the venv's `imp` is the copy, not the real repo. Fail
    # loudly BEFORE running the (expensive, live) workflow.
    probe = subprocess.run(
        [str(venv_python), "-c", "import imp, sys; sys.stdout.write(imp.__file__)"],
        capture_output=True, text=True, check=True,
    )
    resolved = Path(probe.stdout.strip()).resolve()
    assert str(resolved).startswith(str(tmp_path.resolve())), (
        f"venv imp resolves to {resolved}, not under the temp copy {tmp_path}; "
        f"import isolation failed — the host editable repo is shadowing it."
    )
    return venv_python


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

    # Seed the project: copy imp source, git init, run_init, isolated venv.
    venv_python = _seed_dogfood_project(tmp_path, provider)

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
        f"  fast:        {role_line}\n"
        f"  balanced:    {role_line}\n"
        f"  powerful:    {role_line}\n"
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
        f"anything else. Land ALL of these edits (the import and the print) "
        f"in a SINGLE atomic git commit — do not split them across multiple "
        f"commits."
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

    # Make every descendant (bash tool, make, pytest) resolve python3 to the
    # isolated venv. run-tests.sh runs `make test` BEFORE it reads IMP_PYTHON,
    # and the copy's Makefile uses $(PYTHON) ?= python3 from PATH — so PATH is
    # the load-bearing channel. IMP_PYTHON is set too for the non-Makefile branch.
    venv_bin = str(venv_python.parent)
    env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
    env["IMP_PYTHON"] = str(venv_python)

    master, slave = pty.openpty()
    # Propagate a real terminal size onto the slave so imp's renderer wraps to a
    # sensible width instead of flooring to 8 chars under a winsize-0 PTY (issue
    # 2). Query the controlling terminal; fall back to 100x30 when stdout is not
    # a TTY (CI) for determinism.
    try:
        cols, rows = os.get_terminal_size(sys.stdout.fileno())
    except (OSError, ValueError):
        cols, rows = 100, 30
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, winsize)
    # imp answers gates with line input ("1\n") — the only mode now that the
    # TUI is gone. The PTY itself is still needed so the text path's
    # prompt_toolkit prompt has a TTY. Mirrors test_human_gate_pty.
    proc = subprocess.Popen(
        [str(venv_python), "-m", "imp", "new", title],
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
    EVENTS_FIRST_APPEAR_TIMEOUT = 30.0
    TRACE_PTY = os.environ.get("IMP_PTY_TRACE") == "1"

    buf = bytearray()
    gates_answered = 0
    asks_answered = 0
    events_seen_bytes = 0
    ask_questions: list[str] = []
    start_time = time.time()
    deadline = start_time + TOTAL_TIMEOUT
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
                if etype == "human_prompt_pending":
                    phase = payload.get("phase", "?")
                    if not any(p == phase for p, _ in expected_gates):
                        proc.kill()
                        proc.wait()
                        pytest.fail(
                            f"engine emitted human_prompt_pending for phase "
                            f"{phase!r} not in yaml-declared human gates "
                            f"{expected_gates}; workflow.yaml/parse drift?"
                        )
                    os.write(master, b"1\n")
                    gates_answered += 1
                elif etype == "awaiting_user_input":
                    # Interactive agent ended its turn with a question (no tool
                    # call). The engine emits this immediately before blocking on
                    # user_chat_input_fn (executor.py _interactive), so writing
                    # the answer now lands the bytes before prompt_async reads
                    # them. This is provider-agnostic — unlike llm_response
                    # stop_reason, which claude_code logs as None on every turn.
                    question = payload.get("prompt") or ""
                    if question:
                        ask_questions.append(question)
                    os.write(
                        master,
                        b"looks good, proceed with reasonable defaults\n",
                    )
                    asks_answered += 1
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
        "interactive spec phase should produce at least one end-of-turn "
        "question (an awaiting_user_input event); saw 0"
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

    # ── No rework (authoritative) ──────────────────────────────────────
    # The direct signal that something unexpected happened is a REWORK
    # navigation EDGE — NOT a raw count of `step_started` (which re-emits on
    # every agent invocation, including self-healed backoff retries). The robust
    # signal is the rework EDGE: a step_completed whose action targets the
    # implement phase / coder step via goto/resume. A clean single-task run
    # produces NONE (run-tests passes → continue, reviewer approves → continue).
    #
    # Rework edges per workflow.yaml (verified against source):
    #   - run-tests: {else: resume: coder}
    #       → step_completed payload action = {"verb":"goto","target":"coder","resume":True}
    #   - code-reviewer: {if verdict rework, goto: implement}
    #       → step_completed payload action = {"verb":"goto","target":"implement","resume":False}
    #   - merge-gate: {label: Back to implement, goto: implement}
    #       → step_completed payload action = {"verb":"goto","target":"implement","resume":False}
    # Both edge types use verb="goto"; `resume:` edges encode as goto+resume=True
    # (verified against _action_payload in src/imp/engine/executor.py).
    all_events = _read_all_events(events_file)
    rework_edges = [
        e for e in all_events
        if e.get("type") == "step_completed"
        and (e.get("payload") or {}).get("action", {}).get("verb") == "goto"
        and (e.get("payload") or {}).get("action", {}).get("target")
            in {"implement", "coder"}
    ]
    assert not rework_edges, (
        f"REWORK/retry edge(s) detected (goto implement / resume coder) — "
        f"a clean single-task run has none: "
        f"{[(e.get('payload') or {}).get('step') for e in rework_edges]}"
    )

    # ── Clean working tree ─────────────────────────────────────────────
    # Catches uncommitted /project edits and stray engine state. Holds on a
    # healthy run because the seed runs run_init BEFORE the initial commit and
    # Fix D ignores `.imp/` wholesale, so `.imp/` never dirties the tree.
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_path, capture_output=True, text=True, timeout=10, check=True,
    )
    assert not porcelain.stdout.strip(), (
        f"working tree not clean after dogfood run:\n{porcelain.stdout}"
    )

    # ── No engine state in history ─────────────────────────────────────
    committed = subprocess.run(
        ["git", "log", "--name-only", "--pretty=format:%H", "--reverse"],
        cwd=tmp_path, capture_output=True, text=True, timeout=10, check=True,
    )
    imp_paths = [ln for ln in committed.stdout.splitlines()
                 if ln.startswith(".imp/")]
    assert not imp_paths, (
        f"engine state committed to history (should be gitignored): {imp_paths}"
    )

    # ── Commit count ───────────────────────────────────────────────────
    # The task title explicitly instructs the coder to land ALL edits (the
    # import + the print) in a SINGLE atomic commit, so a clean run is seed
    # + one coder commit == 2. The no-rework EDGE assertion above remains the
    # AUTHORITATIVE signal for the original bug (a rework LOOP committing
    # .imp/**); this count is a tighter corroborating check that also catches
    # the coder splitting one logical task across multiple commits (which the
    # single-commit instruction is meant to prevent). If a future dogfood task
    # legitimately spans multiple commits, relax this to `>= 2` and lean on the
    # no-rework + no-.imp-in-history gates.
    log_oneline = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=tmp_path, capture_output=True, text=True, timeout=10, check=True,
    )
    commit_count = len([
        ln for ln in log_oneline.stdout.splitlines() if ln.strip()
    ])
    assert commit_count == 2, (
        f"expected exactly 2 commits (seed + coder's single atomic commit), "
        f"got {commit_count} — the coder split the task across commits or a "
        f"rework/retry loop produced extras:\n{log_oneline.stdout}"
    )

    # Artifacts present.
    artifacts_dir = tmp_path / ".imp" / "changes" / "1" / "artifacts"
    for name in ("spec.md", "plan.md"):
        path = artifacts_dir / name
        assert path.exists(), f"expected artifact {path} after dogfood run"

    # ── imp log smoke ──────────────────────────────────────────────────

    log_out = subprocess.run(
        [str(venv_python), "-m", "imp", "log", "1"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert log_out.returncode == 0, (
        f"imp log rc={log_out.returncode}\nstderr: {log_out.stderr!r}"
    )
    assert log_out.stdout.strip(), "imp log produced empty stdout"

    log_full = subprocess.run(
        [str(venv_python), "-m", "imp", "log", "1", "--full"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert log_full.returncode == 0, (
        f"imp log --full rc={log_full.returncode}\nstderr: {log_full.stderr!r}"
    )
    assert log_full.stdout.strip(), "imp log --full produced empty stdout"

    assert "tools" in log_out.stdout
    assert "tool_call" in log_full.stdout
    assert "    tools  " not in log_full.stdout
