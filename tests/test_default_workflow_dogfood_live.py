"""Dogfood end-to-end test: imp on imp's own source.

This is the integration test that proves the shipped default workflow runs
end-to-end against a real LLM on a real (non-trivial) project — imp's own
source tree. The implementer agent (dispatched by the implement phase's
task-lead) makes a small, deterministic edit (a sentinel print at the top
of the bash tool's function); we verify the workflow ran to completion, all
events fired correctly, and the sentinel landed in the expected file.

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

Recursion guard: the coder runs `make test` -> `pytest -q` in the copied
tree, which would re-collect this file. Scrubbing IMP_LIVE_PROVIDERS from
the subprocess env causes all live tests to skip via opted_in().
"""

from __future__ import annotations

import contextlib
import json
import os
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

from imp.engine import journal
from tests._live import _await_worktree, build_isolated_venv, first_opted_in

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
    ignored = {n for n in names
               if n in {".venv", ".git", ".imp", ".pytest_cache",
                        ".mypy_cache", ".ruff_cache", ".coverage",
                        "htmlcov", ".tox", "__pycache__", "dist", "build"}
               or n.endswith(".egg-info")}
    # The repo carries its own top-level imp/ dogfood control dir (config.yaml,
    # specs/, plans/, workflows/). Copying it into the seed collides with
    # run_init's fresh scaffold ("imp/ already exists; refusing to re-init").
    # Exclude it ONLY at the project root — never src/imp (matched by basename).
    if Path(_dir).resolve() == PROJECT_ROOT:
        ignored.add("imp")
    return ignored


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

    # FULLY isolated venv: NO --system-site-packages, so the host's editable
    # `imp` cannot shadow the copy. uv installs the copy + its deps fast; the
    # one-time dep install at seed is negligible against the ~25-min live run.
    venv_python = build_isolated_venv(tmp_path, extras=("dev",))

    # Pre-flight: prove the venv's `imp` is the COPY (the edit root), not the
    # host repo — and probe via the SAME interpreter and cwd the agent's
    # run-tests uses, so the guard checks the invocation that actually bites.
    # IMP_PROJECT_ROOT == tmp_path for the dogfood, so "under the edit root"
    # means under tmp_path/src/imp.
    probe = subprocess.run(
        [str(venv_python), "-c", "import imp, sys; sys.stdout.write(imp.__file__)"],
        cwd=tmp_path,
        capture_output=True, text=True, check=True,
    )
    resolved = Path(probe.stdout.strip()).resolve()
    edit_root_pkg = (tmp_path / "src" / "imp").resolve()
    assert resolved.is_relative_to(edit_root_pkg), (
        f"venv imp resolves to {resolved}, not under the edit root "
        f"{edit_root_pkg}; import isolation failed — the host editable repo "
        f"is shadowing it."
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
]


# ── Test ───────────────────────────────────────────────────────────────────


def test_dogfood_imp_on_imp_default_workflow(tmp_path: Path) -> None:
    """Run the shipped default workflow against imp's own source.

    Dogfoods imp by asking the workflow to add a unique sentinel print to
    src/imp/agents/tools/bash.py. Launched via --non-interactive with stdin=PIPE:
    the feature intent is written to stdin then stdin is closed so all subsequent
    asks use the EOF default. After completion, verifies the sentinel landed in
    the right file plus all the events.jsonl/imp-log invariants.
    """
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

    wf_yaml = tmp_path / "imp" / "workflows" / "default" / "workflow.yaml"
    expected_gates = _walk_yaml_human_gates(wf_yaml)
    # The superpowers workflow has NO ask/options human gates — the only gate
    # (final-review) is an interactive AGENT, and it is disabled here via
    # vars.final_review=false. So expected_gates is empty by design.
    assert expected_gates == [], (
        f"superpowers workflow should declare no ask/options gates; got {expected_gates}"
    )

    # Override config.yaml to pin the exact model string. run_init wrote
    # provider-default models; we want determinism across run_init updates.
    (tmp_path / "imp" / "config.yaml").write_text(
        f"providers: [{provider}]\n"
        "tiers:\n"
        f"  low:    {{{provider}: {model}}}\n"
        f"  medium: {{{provider}: {model}}}\n"
        f"  high:   {{{provider}: {model}}}\n"
        # Opt this run into the lint gate (off by default): exercises the
        # IMP_LINT_COMMAND pre-commit gate so the exemplar's deliberate F401
        # unused import is caught (Acceptance #3/#6). Top-level keys, parsed into
        # Config.lint_command / test_command — NOT workflow vars.
        "lint_command: make lint\n"
        "test_command: make test\n"
        # Plan 1 config vars override: skip the interactive final gate so the
        # change auto-merges deterministically (the spec phase still drives one
        # interactive round). Exercises the final-review-gate branch -> merge.
        "vars:\n"
        "  final_review: false\n"
    )
    # imp/config.yaml is tracked (run_init wrote it, the seed commit captured
    # it), so fold the model-pin override back into the seed commit — otherwise
    # it leaves the working tree dirty and trips the clean-tree gate below.
    subprocess.run(["git", "add", "imp/config.yaml"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git",
         "-c", "user.email=test@imp",
         "-c", "user.name=imp-dogfood",
         "commit", "--amend", "--no-edit"],
        cwd=tmp_path, check=True, capture_output=True,
    )

    # The spec-writer now ALWAYS opens with a clarifying question (its prompt
    # mandates a turn-1 ask, no tools, no exploration). The system prompt no
    # longer surfaces the title, so the agent learns the task from the FIRST
    # awaiting_user_input reply — that is where the driver delivers the title
    # text below. With --non-interactive, the intent is written to stdin and
    # stdin is closed; all subsequent asks use the EOF default (first option).

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
        f"commits. This is a trivial two-line change: do NOT create, modify, "
        f"or delete any test files; do NOT run pytest, make, or any "
        f"diagnostic/verification scripts; do NOT touch any file other than "
        f"src/imp/agents/tools/bash.py. After making the edit and committing, "
        f"complete the step immediately — no verification is needed."
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

    # Launch imp new via --non-interactive with stdin=PIPE (no PTY needed).
    # Deliver the feature intent as the FIRST scripted reply (the spec-writer's
    # opening ask), then close stdin so every later ask falls to the EOF default.
    proc = subprocess.Popen(
        [str(venv_python), "-m", "imp", "new", title, "--non-interactive"],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
        text=False,
    )
    with contextlib.suppress(BrokenPipeError):
        proc.stdin.write(title.encode("utf-8") + b"\n")
        proc.stdin.close()

    TOTAL_TIMEOUT = 1800.0
    EVENTS_FIRST_APPEAR_TIMEOUT = 30.0

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

    def _teardown_proc() -> None:
        signal.signal(signal.SIGINT, _prev_sigint)
        if proc.poll() is None:
            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.kill()
            proc.wait()

    # Discover the Change worktree (created at startup, before any LLM call).
    # Capture the slug NOW — the worktree is transient and is removed at merge.
    # Guarded so a startup-crash timeout still tears down proc.
    try:
        change_wt = _await_worktree(tmp_path, timeout=EVENTS_FIRST_APPEAR_TIMEOUT)
    except BaseException:
        _teardown_proc()
        raise
    slug = change_wt.name
    events_file = change_wt / ".imp" / "change" / "events.jsonl"

    gates_answered = 0
    events_seen_bytes = 0
    all_events_accum: list[dict[str, Any]] = []
    start_time = time.time()
    deadline = start_time + TOTAL_TIMEOUT
    events_first_seen_at: float | None = None

    print(
        f"\n[dogfood] provider={provider} model={model} starting; "
        f"sentinel={sentinel} expected_gates={len(expected_gates)}",
        file=sys.stderr, flush=True,
    )

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
            # Startup guard: fail only if events were NEVER seen. We key off
            # events_first_seen_at (set by _tail_events on first sighting), not
            # events_file.exists() — at merge the worktree (and events.jsonl) is
            # removed, so a "currently absent" check would misfire on a healthy
            # run that has merged, reporting a bogus "did not initialize".
            if (
                events_first_seen_at is None
                and time.time() - start_time > EVENTS_FIRST_APPEAR_TIMEOUT
            ):
                proc.kill()
                proc.wait()
                pytest.fail(
                    f"events.jsonl never appeared within "
                    f"{EVENTS_FIRST_APPEAR_TIMEOUT}s of process start; "
                    f"engine did not initialize."
                )

            for ev in _tail_events():
                all_events_accum.append(ev)
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
                    gates_answered += 1
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
                        f"gates_answered={gates_answered}/{len(expected_gates)}"
                    )

            if proc.poll() is not None:
                break

            time.sleep(0.05)
        else:
            proc.kill()
            proc.wait()
            pytest.fail(
                f"dogfood workflow exceeded {TOTAL_TIMEOUT}s wall clock "
                f"(may be reviewer flake or coder iteration storm; "
                f"rerun once before investigating).\n"
                f"gates_answered={gates_answered}/{len(expected_gates)}"
            )
    finally:
        _teardown_proc()

    rc = proc.wait(timeout=10)
    assert rc == 0, (
        f"expected exit 0, got {rc}; "
        f"gates_answered={gates_answered}/{len(expected_gates)}"
    )

    # Resolve the FINAL slug from the journal. The spec-writer may RENAME the
    # change (a spec-phase self-resume edge), so `change_wt.name` captured at
    # startup is the PRE-rename slug and the live worktree (with events.jsonl)
    # was renamed/removed mid-run — the journal is keyed under the final slug.
    # Exactly one change ran here.
    journaled_slugs = journal.journal_slugs(tmp_path)
    assert len(journaled_slugs) == 1, (
        f"expected exactly one journaled change, got {journaled_slugs} "
        f"(startup slug was {slug!r})"
    )
    slug = journaled_slugs[0]

    # The worktree was removed at merge; completion status lives in the journal.
    state_raw = journal.journal_show(tmp_path, slug, "state.json")
    assert state_raw is not None, (
        f"no journaled state.json for {slug!r}; merge/journal did not run"
    )
    state = json.loads(state_raw)
    assert state["status"] == "completed", (
        f"expected status=completed, got {state['status']!r}"
    )

    assert gates_answered == 0, (
        f"superpowers workflow has no ask/options gates; saw {gates_answered} "
        f"human_prompt_pending events"
    )

    # ── Dogfood-specific verification ──────────────────────────────────

    # Sentinel landed in the target file.
    bash_py = tmp_path / "src" / "imp" / "agents" / "tools" / "bash.py"
    assert bash_py.exists(), (
        f"{bash_py} missing after dogfood run — coder deleted the target file?"
    )
    bash_src = bash_py.read_text()
    assert sentinel in bash_src, (
        f"sentinel {sentinel!r} not found in {bash_py}; the implementer either "
        f"skipped the edit or placed the sentinel elsewhere.\n"
        f"Last 500 chars of bash.py:\n{bash_src[-500:]}"
    )

    # ── No rework / no backstop (authoritative) ─────────────────────────
    # A clean run produces NO rework edges. Rework/backstop edges (step_completed
    # whose action is a back-edge), by issuing step → target:
    #   run-tests (fail)        → resume task-lead       (failing suite)
    #   code-reviewer task_gap  → goto   implement
    #   code-reviewer plan_gap  → goto   plan-writer
    #   task-lead  deviation    → resume plan-writer
    #   spec/plan reviewer block→ resume spec-writer / plan-writer
    #   *-commit-verify fail    → resume spec-writer / plan-writer
    #   final-review rework     → goto   implement   (disabled via final_review=false)
    # NOT rework: task-lead's own `goto task-lead` self-loop (one per task) — it
    # targets task-lead but is ISSUED BY task-lead, so exclude that exact pair.
    # NOT rework: the three fast-path gates (Task 9), all FORWARD skips on a
    # trivial change — `fast-path-spec-gate → plan-writer` skips spec-review,
    # `fast-path-plan-gate → task-lead` skips plan-review, and
    # `fast-path-cycle-gate → review` skips plan-cycle-check. A clean fast-path
    # run issues exactly these, so exclude them by their issuing step. (spec-gate
    # and plan-gate would otherwise match — their targets plan-writer/task-lead
    # are in the set below; cycle-gate's target `review` is not.)
    # NB: BOTH goto and resume serialize as verb "goto" (resume adds resume=True;
    # executor.py:540-541 _action_payload), so matching verb=="goto" catches the
    # resume-based backstops (run-tests/commit-check/reviewer/deviation) too.
    def _is_rework(e: dict[str, Any]) -> bool:
        if e.get("type") != "step_completed":
            return False
        p = e.get("payload") or {}
        act = p.get("action") or {}
        if act.get("verb") != "goto":
            return False
        step, target = p.get("step"), act.get("target")
        if step == "task-lead" and target == "task-lead":
            return False  # normal per-task progress, not rework
        if step in {"fast-path-spec-gate", "fast-path-plan-gate",
                    "fast-path-cycle-gate"}:
            return False  # fast-path forward skips (Task 9), not rework
        # Deterministic spec-phase RENAME flow (fires on every clean run where the
        # spec-writer renames the change, which the spec-writer prompt mandates):
        #   spec-writer -> spec-writer        the rename self-resume (rename_change)
        #   spec-verify-commit -> spec-writer the spec was committed under the OLD
        #     slug, then renamed; spec-commit-verify.sh checks HEAD touches
        #     imp/specs/<NEW slug>.md, fails once, and resumes spec-writer to
        #     re-commit under the new name. Benign, not change-quality rework.
        if (step, target) in {("spec-writer", "spec-writer"),
                              ("spec-verify-commit", "spec-writer")}:
            return False
        return target in {"implement", "task-lead", "plan-writer", "spec-writer"}

    # Use the COMPLETE journaled event stream (authoritative): the live
    # all_events_accum stops at the worktree rename, so it can miss late edges.
    events_raw = journal.journal_show(tmp_path, slug, "events.jsonl")
    assert events_raw is not None, f"no journaled events.jsonl for {slug!r}"
    full_events = [
        json.loads(ln)
        for ln in events_raw.decode("utf-8").splitlines()
        if ln.strip()
    ]
    rework_edges = [e for e in full_events if _is_rework(e)]
    rework_pairs = [
        (
            (e.get("payload") or {}).get("step"),
            (e.get("payload") or {}).get("action", {}).get("target"),
        )
        for e in rework_edges
    ]
    assert not rework_edges, (
        f"rework/backstop edge(s) detected — a clean single-task run has none: "
        f"{rework_pairs}"
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

    # ── Single atomic coder commit ─────────────────────────────────────
    # Measured on the base branch after merge: exactly one non-merge commit
    # MODIFIES the sentinel file. --no-merges excludes the --no-ff merge commit;
    # --diff-filter=M excludes the seed commit (which ADDS bash.py via the
    # copytree of imp's own source). What remains is the coder's single atomic
    # commit, which modifies src/imp/agents/tools/bash.py.
    coder_commits = subprocess.run(
        ["git", "log", "--no-merges", "--oneline", "--diff-filter=M", "--",
         "src/imp/agents/tools/bash.py"],
        cwd=tmp_path, capture_output=True, text=True, timeout=10, check=True,
    )
    n_coder = len([ln for ln in coder_commits.stdout.splitlines() if ln.strip()])
    assert n_coder == 1, (
        f"expected exactly 1 non-merge commit modifying bash.py (the coder's "
        f"single atomic commit), got {n_coder}:\n{coder_commits.stdout}"
    )

    # Artifacts present: the default workflow writes spec/plan INTO the worktree
    # and commits them; merge-in-place lands them on the base branch at tmp_path.
    # New layout (spec §12): docs/ layer dropped; plans are numbered.
    spec_path = tmp_path / "imp" / "specs" / f"{slug}.md"
    assert spec_path.exists(), f"expected spec artifact {spec_path} after run"
    plans = sorted((tmp_path / "imp" / "plans").glob(f"{slug}-plan-*.md"))
    assert plans, f"expected at least one imp/plans/{slug}-plan-N.md after run"

    # ── imp log smoke (journal-read path: worktree is gone) ────────────
    log_out = subprocess.run(
        [str(venv_python), "-m", "imp", "log", slug],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert log_out.returncode == 0, (
        f"imp log rc={log_out.returncode}\nstderr: {log_out.stderr!r}"
    )
    assert log_out.stdout.strip(), "imp log produced empty stdout"

    log_full = subprocess.run(
        [str(venv_python), "-m", "imp", "log", slug, "--full"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert log_full.returncode == 0, (
        f"imp log --full rc={log_full.returncode}\nstderr: {log_full.stderr!r}"
    )
    assert log_full.stdout.strip(), "imp log --full produced empty stdout"

    assert "tools" in log_out.stdout
    assert "tool_call" in log_full.stdout
    assert "    tools  " not in log_full.stdout

    # ── imp status smoke: the completed slug appears under Completed Changes ──
    status_out = subprocess.run(
        [str(venv_python), "-m", "imp", "status"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert status_out.returncode == 0, (
        f"imp status rc={status_out.returncode}\nstderr: {status_out.stderr!r}"
    )
    assert "Completed Changes" in status_out.stdout, status_out.stdout
    assert slug in status_out.stdout, status_out.stdout
