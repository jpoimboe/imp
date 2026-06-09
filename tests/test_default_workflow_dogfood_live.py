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
  5. The headless `--message` new→resume loop drives a multi-solicit
     workflow to completion (one reply per invocation).
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
        # codex refuses *-codex models (preflight M1); gpt-5.5 is the sanctioned
        # non-codex model. Auth is via the `codex` CLI's own ~/.codex/auth.json.
        "codex": "gpt-5.5",
    }[provider]


def _ignore_for_copy(_dir: str, names: list[str]) -> set[str]:
    ignored = {n for n in names
               if n in {".venv", ".git", ".imp", ".pytest_cache",
                        ".mypy_cache", ".ruff_cache", ".coverage",
                        "htmlcov", ".tox", "__pycache__", "dist", "build"}
               or n.endswith(".egg-info")}
    # The repo carries its own top-level imp/ dogfood control dir (project.yaml,
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

    # run_init MATERIALIZES imp/workflows/default as a real directory (the right
    # thing for a pip-installed user: src/imp/assets/ lives in site-packages).
    # But THIS repo — which the seed is a copy of — commits it as a symlink to
    # the live bundled assets, and ships tests/test_assets_workflow.py::
    # test_dogfood_workflow_install_is_symlink_to_assets pinning exactly that.
    # Left as a directory, that bundled test FAILS inside the coder's `make test`
    # (run-tests gate), which is unrelated to the coder's change and was poisoning
    # the gate -> a deterministic run-tests rework + minutes of the agent flailing
    # over a "pre-existing failure". The seed already copied the symlink target
    # (only the top-level imp/ is excluded, not src/), so restore the symlink form
    # to match the source repo this dogfood claims to run against.
    wf_install = tmp_path / "imp" / "workflows" / "default"
    if wf_install.is_dir() and not wf_install.is_symlink():
        shutil.rmtree(wf_install)
        wf_install.symlink_to(Path("../../src/imp/assets/workflows/default"))

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
    # Scrub PYTHONPATH: `make test-dogfood` prepends PYTHONPATH=<checkout>/src
    # (the worktree-safety pin in the Makefile). That host path would shadow the
    # isolated venv's editable `imp`, defeating the very isolation this probe
    # asserts. Isolation must not depend on the caller's env, so drop it here
    # (and for the engine launch below).
    probe_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    probe = subprocess.run(
        [str(venv_python), "-c", "import imp, sys; sys.stdout.write(imp.__file__)"],
        cwd=tmp_path,
        capture_output=True, text=True, check=True,
        env=probe_env,
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


def test_dogfood_imp_on_imp_default_workflow(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the shipped default workflow against imp's own source.

    Dogfoods imp by asking the workflow to add a unique sentinel print to
    src/imp/agents/tools/bash.py. Driven headlessly via the `--message`
    new→resume loop: `imp new <title> --non-interactive --message <intent>`
    delivers the feature intent to the spec-writer's opening wait_for_user;
    each later wait_for_user in a given invocation exhausts the one allotted
    message and PAUSES cleanly, which the driver continues with
    `imp resume <id> --non-interactive --message <proceed_reply>` — one reply
    per invocation — until the change completes and merges. After completion,
    verifies the sentinel landed in the right file plus all the
    events.jsonl/imp-log invariants.
    """
    # Isolate XDG_CONFIG_HOME to a tmp dir BEFORE anything reads/writes user
    # config. _seed_dogfood_project() runs run_init() in-process, which now
    # bootstraps the USER config (~/.config/imp/config.yaml) — without this the
    # machine has none and run_init WOULD create the real one. The later
    # subprocess inherits this via `env = dict(os.environ)`, so both the
    # in-process init and the subprocess read the isolated user layer.
    # user_config_path() reads XDG_CONFIG_HOME at call time, so setting it here
    # (before run_init) takes effect.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".xdg"))

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
    elif provider == "codex":
        # Like claude_code: the codex CLI handles auth via its own
        # ~/.codex/auth.json (codex login) — no env key. Require both the CLI
        # and an auth file (the runner's preflight asserts auth too).
        assert shutil.which("codex") is not None, (
            "codex opted in but `codex` CLI not on PATH"
        )
        assert (Path.home() / ".codex" / "auth.json").exists(), (
            "codex opted in but ~/.codex/auth.json missing (run `codex login`)"
        )

    # Seed the project: copy imp source, git init, run_init, isolated venv.
    venv_python = _seed_dogfood_project(tmp_path, provider)

    # Override imp/project.yaml (the project layer) to pin the exact model
    # string. run_init wrote project.yaml with workflow+vars but NO model
    # bindings (the floor owns provider-default models now); we replace it with a
    # complete project layer for determinism across run_init updates.
    (tmp_path / "imp" / "project.yaml").write_text(
        "workflow: default\n"
        f"providers: [{provider}]\n"
        "tiers:\n"
        f"  low:    {{{provider}: {model}}}\n"
        f"  medium: {{{provider}: {model}}}\n"
        f"  high:   {{{provider}: {model}}}\n"
        # Opt this run into the lint gate (off by default): exercises the
        # IMP_LINT_COMMAND pre-commit gate so the exemplar's deliberate F401
        # unused import is caught (Acceptance #3/#6). Project-layer keys, parsed
        # by parse_config_layer into Config.lint_command / test_command — NOT
        # workflow vars.
        "lint_command: make lint\n"
        "test_command: make test\n"
        # Plan 1 config vars override: skip the interactive final gate so the
        # change auto-merges deterministically (the spec phase still drives one
        # interactive round). Exercises the final-review-gate branch -> merge.
        "vars:\n"
        "  final_review: false\n"
    )
    # imp/project.yaml is tracked (run_init wrote it, the seed commit captured
    # it), so fold the model-pin override back into the seed commit — otherwise
    # it leaves the working tree dirty and trips the clean-tree gate below.
    subprocess.run(["git", "add", "imp/project.yaml"], cwd=tmp_path, check=True)
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
    # wait_for_user reply — that is the FIRST `--message` (the intent) below.
    # In --non-interactive mode each invocation carries ONE `--message`: the
    # first wait_for_user solicit consumes it; any later solicit in the same
    # invocation has no message left and the run PAUSES (clean, resumable). The
    # driver loop continues with `imp resume <id> --message <proceed_reply>`.

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
    # Drop the host PYTHONPATH (`make test-dogfood` pins it to <checkout>/src):
    # the engine and every descendant must resolve `imp` from the isolated venv
    # copy, not the host source tree. Same isolation rationale as the seed probe.
    env.pop("PYTHONPATH", None)
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

    # Opt-in capture (diagnostics): set IMP_DOGFOOD_CAPTURE=<dir> to persist the
    # workflow subprocess output AND a copy of the journal (.imp/) OUTSIDE
    # tmp_path, so they survive both pytest's tmp-dir GC and an assertion
    # failure. Without it the run is silent (stdout DEVNULL) as before. The
    # finalizer runs even when a later assertion (rc/status/sentinel/rework)
    # fails — that is where the real run-tests failure detail lives.
    capture_dir = os.environ.get("IMP_DOGFOOD_CAPTURE")
    capture_path = Path(capture_dir) if capture_dir else None
    cap_out = None
    if capture_path is not None:
        capture_path.mkdir(parents=True, exist_ok=True)
        cap_out = (capture_path / "workflow.out").open("wb")

        def _dump_capture() -> None:
            if cap_out is not None and not cap_out.closed:
                cap_out.flush()
                cap_out.close()
            journal_src = tmp_path / ".imp"
            if journal_src.exists():
                with contextlib.suppress(OSError, shutil.Error):
                    shutil.copytree(
                        journal_src, capture_path / "imp_journal",
                        dirs_exist_ok=True,
                    )

        request.addfinalizer(_dump_capture)

    TOTAL_TIMEOUT = 1800.0
    EVENTS_FIRST_APPEAR_TIMEOUT = 30.0
    MAX_RESUMES = 8

    # The FIRST `--message` is the feature intent (the spec-writer's opening
    # wait_for_user consumes it). Any LATER wait_for_user in the same invocation
    # has no message left and pauses; the driver resumes with this firm generic
    # continuation so the workflow proceeds without further human input.
    intent = title
    proceed_reply = (
        "Proceed using your best judgment. The requirements in my first "
        "message are complete and final — implement exactly as specified and "
        "do not ask further questions."
    )

    # SIGINT handling: hard-kill the CURRENTLY-running subprocess PG so Ctrl-C
    # is unambiguous (raising KeyboardInterrupt from a signal handler is
    # unreliable inside pytest — some plugins swallow it). `current_proc` is a
    # one-element mutable holder updated on each launch so the handler/teardown
    # always target the live invocation.
    current_proc: list[subprocess.Popen[bytes] | None] = [None]

    def _on_sigint(_signum: int, _frame: Any) -> None:
        with contextlib.suppress(OSError):
            os.write(2, b"\n[dogfood] SIGINT received; killing subprocess and aborting\n")
        p = current_proc[0]
        if p is not None:
            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(p.pid, signal.SIGKILL)
        os._exit(130)
    _prev_sigint = signal.signal(signal.SIGINT, _on_sigint)

    def _teardown_proc() -> None:
        signal.signal(signal.SIGINT, _prev_sigint)
        p = current_proc[0]
        if p is not None and p.poll() is None:
            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(p.pid, signal.SIGKILL)
            p.kill()
            p.wait()

    stdout_sink = cap_out if cap_out is not None else subprocess.DEVNULL
    stderr_sink = subprocess.STDOUT if cap_out is not None else subprocess.DEVNULL

    def _launch(argv: list[str]) -> subprocess.Popen[bytes]:
        """Launch one headless invocation (new or resume) and record it as the
        current proc for the SIGINT handler/teardown. stdin is DEVNULL — the
        reply channel is `--message`, not stdin."""
        p: subprocess.Popen[bytes] = subprocess.Popen(
            argv,
            cwd=tmp_path,
            stdin=subprocess.DEVNULL,
            stdout=stdout_sink,
            stderr=stderr_sink,
            env=env,
            start_new_session=True,
            text=False,
        )
        current_proc[0] = p
        return p

    events_seen_bytes = 0
    all_events_accum: list[dict[str, Any]] = []
    start_time = time.time()
    deadline = start_time + TOTAL_TIMEOUT
    events_first_seen_at: float | None = None

    print(
        f"\n[dogfood] provider={provider} model={model} starting; "
        f"sentinel={sentinel}",
        file=sys.stderr, flush=True,
    )

    # events_file is bound after the worktree is discovered (below) and is
    # appended-to across every resume invocation — the byte cursor is therefore
    # continuous across the whole loop.
    events_file: Path | None = None

    def _tail_events() -> list[dict[str, Any]]:
        nonlocal events_seen_bytes, events_first_seen_at
        if events_file is None or not events_file.exists():
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

    def _drive_until_exit(
        proc: subprocess.Popen[bytes], *, startup_guard: bool
    ) -> None:
        """Drain events and poll until `proc` exits on its own.

        Fail-fast on a NON-null-reason workflow_paused (max_loops/agent error/
        max_retries exhaustion). An awaiting-user pause carries reason=None
        (executor.py awaiting-user Pause()) — that is NOT a failure: the proc
        will exit cleanly (rc 0) and the driver loop resumes with --message.
        Enforces the GLOBAL wall-clock deadline across the whole new→resume loop.
        """

        def _drain_and_check_pause() -> None:
            # reason is None  -> awaiting-user pause (resumable): NOT a failure;
            #                    keep draining until the proc exits.
            # reason non-null -> real failure (max_loops / error category /
            #                    max_retries exhaustion): fail-fast.
            for ev in _tail_events():
                all_events_accum.append(ev)
                if ev.get("type") != "workflow_paused":
                    continue
                payload = ev.get("payload") or {}
                if payload.get("reason") is not None:
                    with contextlib.suppress(OSError, ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    proc.kill()
                    proc.wait()
                    pytest.fail(
                        f"engine emitted workflow_paused with non-null reason "
                        f"({payload.get('reason')!r}); likely max_loops or "
                        f"max_retries exhausted in a step or phase."
                    )

        while proc.poll() is None:
            if (
                startup_guard
                and events_first_seen_at is None
                and time.time() - start_time > EVENTS_FIRST_APPEAR_TIMEOUT
            ):
                with contextlib.suppress(OSError, ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.kill()
                proc.wait()
                pytest.fail(
                    f"events.jsonl never appeared within "
                    f"{EVENTS_FIRST_APPEAR_TIMEOUT}s of process start; "
                    f"engine did not initialize."
                )

            _drain_and_check_pause()

            if time.time() >= deadline:
                with contextlib.suppress(OSError, ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.kill()
                proc.wait()
                pytest.fail(
                    f"dogfood workflow exceeded {TOTAL_TIMEOUT}s wall clock "
                    f"(may be reviewer flake or coder iteration storm; "
                    f"rerun once before investigating)."
                )

            time.sleep(0.05)

        # Final drain after exit: the terminal workflow_paused is written just
        # before the proc exits, so the while-guard can miss it. Read it now so a
        # real-failure pause fails fast here (precise diagnostic) instead of being
        # misclassified as awaiting-user and burning resume cycles to the cap.
        _drain_and_check_pause()

    change_id: str | None = None
    slug = "(unknown)"
    try:
        # ── First invocation: imp new --message <intent> ──────────────────
        proc = _launch(
            [str(venv_python), "-m", "imp", "new", title,
             "--non-interactive", "--message", intent]
        )

        # Discover the Change worktree (created at startup, before any LLM call).
        # The PATH is stable across the spec-writer's rename, so this stays valid
        # until merge. Read the IMMUTABLE id from change.json — the slug changes
        # on rename, but the id is the stable resume token for ALL resumes.
        change_wt = _await_worktree(tmp_path, timeout=EVENTS_FIRST_APPEAR_TIMEOUT)
        slug = change_wt.name
        events_file = change_wt / ".imp" / "change" / "events.jsonl"
        state_file = change_wt / ".imp" / "change" / "state.json"
        change_id = json.loads(
            (change_wt / ".imp" / "change" / "change.json").read_text()
        )["id"]

        _drive_until_exit(proc, startup_guard=True)
        rc = proc.wait(timeout=10)
        # rc 0 covers BOTH a clean awaiting-user pause and completion.
        assert rc == 0, f"new exit {rc}"

        # ── Resume loop: one --message per invocation until completion ─────
        resumes = 0
        while True:
            # A completing invocation removes the worktree at merge, so check
            # existence BEFORE reading state_file.
            if not change_wt.exists():
                break  # merged/completed -> verify via journal below
            try:
                state = json.loads(state_file.read_text())
            except FileNotFoundError:
                break  # worktree torn down mid-read -> merged/completed
            status = state.get("status")
            if status == "completed":
                break
            if status == "paused":
                # Reached only on an awaiting-user pause: a non-null-reason
                # pause already fail-fasted inside _drive_until_exit.
                resumes += 1
                if resumes > MAX_RESUMES:
                    pytest.fail(
                        f"exceeded {MAX_RESUMES} resume cycles; workflow keeps "
                        f"soliciting (status={status!r}, step={state.get('step')!r})"
                    )
                proc = _launch(
                    [str(venv_python), "-m", "imp", "resume", change_id,
                     "--non-interactive", "--message", proceed_reply]
                )
                _drive_until_exit(proc, startup_guard=False)
                rc = proc.wait(timeout=10)
                assert rc == 0, f"resume exit {rc}"
                continue
            pytest.fail(
                f"unexpected paused state status={status!r} "
                f"step={state.get('step')!r}"
            )
    except BaseException:
        _teardown_proc()
        raise
    finally:
        _teardown_proc()

    # Resolve the change id from the journal. The id is immutable; read it back
    # from the journal to assert exactly one change ran here. The journal is
    # keyed under the id (== change_id used to drive the resume loop above).
    journaled_ids = journal.journal_ids(tmp_path)
    assert len(journaled_ids) == 1, (
        f"expected exactly one journaled change, got {journaled_ids} "
        f"(startup worktree slug was {slug!r}, change_id was {change_id!r})"
    )
    slug = journaled_ids[0]

    # The worktree was removed at merge; completion status lives in the journal.
    state_raw = journal.journal_show(tmp_path, slug, "state.json")
    assert state_raw is not None, (
        f"no journaled state.json for {slug!r}; merge/journal did not run"
    )
    state = json.loads(state_raw)
    assert state["status"] == "completed", (
        f"expected status=completed, got {state['status']!r}"
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
    # all_events_accum may miss late edges (the rename is content-only — title
    # change + spec git mv — and the worktree path stays stable across it, but
    # accumulation can still lag behind the journaled record).
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
    #
    # Artifact filenames use the change's DERIVED slug, NOT its journal id. The
    # id is the immutable canonical key (`<date>-<random>`, e.g. 2026-06-05-mlysrp)
    # that journal_ids() returns; the slug is `dated_slug(title)` and changes with
    # the spec-writer's mandated rename (change.py: "immutable id identity, derived
    # slug"). They diverge on every real run, so resolve the slug from the journaled
    # title the same way the engine's Change.slug does.
    from imp.engine.slug import dated_slug
    change_meta = json.loads(
        journal.journal_show(tmp_path, slug, "change.json") or b"{}"
    )
    artifact_slug = dated_slug(change_meta["title"], date=slug[:10])
    spec_path = tmp_path / "imp" / "specs" / f"{artifact_slug}.md"
    assert spec_path.exists(), f"expected spec artifact {spec_path} after run"
    plans = sorted((tmp_path / "imp" / "plans").glob(f"{artifact_slug}-plan-*.md"))
    assert plans, (
        f"expected at least one imp/plans/{artifact_slug}-plan-N.md after run"
    )

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
    # `imp status` lists in-flight Changes only by default; once the change has
    # merged there are none, so `--all` is required to surface the journaled
    # completed Change (main.py _cmd_status: completed rows are added under --all).
    status_out = subprocess.run(
        [str(venv_python), "-m", "imp", "status", "--all"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert status_out.returncode == 0, (
        f"imp status rc={status_out.returncode}\nstderr: {status_out.stderr!r}"
    )
    assert "completed" in status_out.stdout, status_out.stdout
    assert slug in status_out.stdout, status_out.stdout
