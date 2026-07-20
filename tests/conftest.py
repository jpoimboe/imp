"""Shared pytest fixtures."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
_WORKTREE_SRC = str(Path(__file__).parent.parent / "src")

# Live-API tests (the `*_live.py` convention, excluding the unrelated
# `test_live_*` UI tests) drive real provider round-trips that take tens of
# seconds each — 30-45s already at `-n 4` — and stretch further under heavier
# `-n` concurrency. That leaves little headroom under the global `timeout = 60`
# (signal method, pyproject.toml), which then fires SIGALRM mid-call: a spurious
# Timeout on a sync subprocess test, or an event loop torn down mid-await on an
# async one. The resume tests already opt out per-test with `timeout(240)` for
# exactly this reason; generalize it so every live test gets the same backstop
# and the timeout only ever catches a genuine hang. This is what lets the live
# suite parallelize past -n 2 without the cap turning a slow call into a flake.
_LIVE_TEST_TIMEOUT = 240
# impty PTY e2e tests (tests/impty/test_e2e_*.py) drive a full real `imp` binary
# to completion over a pty. Under `-n auto` oversubscription — most visibly from
# inside a live imp/dogfood session — pyte's pure-Python VT parse plus the run's
# own work crosses the global `timeout = 60` (signal method, pyproject.toml),
# and SIGALRM kills a structurally-sound test. Grant them a generous liveness
# ceiling so the timeout only ever catches a genuine hang; nothing asserts how
# much of it is used. Independent of _LIVE_TEST_TIMEOUT (different rationale)
# though it currently shares the value.
_IMPTY_E2E_TIMEOUT = 240


def pytest_addoption(parser: pytest.Parser) -> None:
    from tests._live import RUN_LIVE_ALL

    parser.addoption(
        "--run-live",
        nargs="?",
        const=RUN_LIVE_ALL,
        default=None,
        help=(
            "Run live-API tests. Bare --run-live runs every provider whose "
            "toolchain/credentials are present; --run-live=claude_code,openai "
            "scopes to a subset (valid: anthropic, openai, claude_code, codex)."
        ),
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item],
) -> None:
    """Two jobs at collection: (1) raise the per-test timeout for the slow
    liveness families — live-API `*_live.py` files (see _LIVE_TEST_TIMEOUT) and
    impty PTY e2e `tests/impty/test_e2e_*.py` files (see _IMPTY_E2E_TIMEOUT) —
    each keeping its own explicit `timeout` marker if it declares one (e.g. the
    resume tests that pick 240); and (2) gate every
    `@pytest.mark.live(<provider>)` test behind --run-live (see tests/_live.py's
    live_skip_reason)."""
    from tests._live import live_skip_reason, parse_run_live

    requested = parse_run_live(config.getoption("--run-live"))
    for item in items:
        path = Path(str(item.fspath))
        name = path.name
        is_live_file = "_live" in name and not name.startswith("test_live_")
        if is_live_file and item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(_LIVE_TEST_TIMEOUT))
        is_impty_e2e = path.parent.name == "impty" and name.startswith("test_e2e_")
        if is_impty_e2e and item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(_IMPTY_E2E_TIMEOUT))
        marker = item.get_closest_marker("live")
        if marker is None:
            continue
        reason = live_skip_reason(requested, marker.args[0])
        if reason is not None:
            item.add_marker(pytest.mark.skip(reason=reason))


def _pin_subprocess_imp_to_working_tree() -> None:
    """Make subprocess `imp` resolution deterministic for the whole test run.

    pyproject's `pythonpath=["src"]` only fixes the *in-process* interpreter.
    Tests that spawn `python -m imp` / `python -c "import imp"` children
    otherwise resolve `imp` from the GLOBAL editable install — shared, mutable
    state that parallel worktrees/sessions repoint or tear down (a `pip install
    -e` mid-reinstall removes the `.pth`, and `import imp` then falls onto the
    repo's `imp/` data dir as a namespace package -> ModuleNotFoundError, or
    onto a sibling worktree's stale code). That made bare-`pytest` subprocess
    failures look like regressions when they were environmental.

    Pinning PYTHONPATH here, at conftest import (before collection spawns
    anything), shadows the global install for every child that inherits
    os.environ — under `pytest`, `python -m pytest`, IDE runners, and `make`
    alike. This supersedes the Makefile's `PYTEST_PYTHONPATH` pin, which only
    covered `make test`. Absolute + prepend-safe + idempotent, mirroring
    engine_env's per-subprocess injection. Scoped to an imp checkout so an
    unrelated project's `src/` is never forced onto its subprocesses' path.

    Relies on PYTHONPATH preceding site-packages on sys.path — true for imp's
    path-style editable install. A strict PEP 660 editable that registers a
    `sys.meta_path` finder would resolve first and defeat it; out of scope while
    imp ships path-style (same caveat as engine_env).
    """
    src = Path(__file__).resolve().parent.parent / "src"
    if not (src / "imp" / "__init__.py").is_file():
        return
    existing = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    if str(src) in existing:
        return
    os.environ["PYTHONPATH"] = os.pathsep.join([str(src), *existing])


_pin_subprocess_imp_to_working_tree()


@pytest.fixture(autouse=True)
def _stub_provider_api_keys(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub provider env vars only if not already set. Live-API tests get
    the real env through (the collection gate handles the "no auth" case);
    ordinary tests get the stub so module-level provider construction works.

    Live-API tests spawn subprocesses (or the claude_code provider's nested
    `claude`) that read ANTHROPIC_API_KEY from the real env. Stubbing it to
    "sk-test" would give the subprocess a fake key and cause "Invalid API key"
    / 401 errors. These tests follow a `_live` suffix convention
    (`*_live.py`, `*_live_<provider>.py`) and gate themselves with the
    `live` marker, so exempt them from stubbing. The unrelated `test_live_*` UI
    tests (live_status/live_driver) are NOT live-API tests and still get the
    stub.
    """
    name = Path(request.node.fspath).name
    if "_live" in name and not name.startswith("test_live_"):
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    if not os.environ.get("OPENAI_API_KEY"):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


@pytest.fixture(autouse=True)
def _reset_theme() -> Generator[None, None, None]:
    """Pin the active color theme to `ansi` around every test. `palette` keeps
    the active theme in module-global state, and any test that runs the CLI
    in-process (e.g. `main(["list"])`) calls `_apply_theme` -> `set_theme(...)`,
    which leaks the resolved theme into the next test. A test that asserts the
    ansi-named SGR codes (e.g. gray `\\x1b[90m`) then fails under random ordering
    if a prior test resolved a dark/light theme — most visibly when a non-ansi
    `theme:` lives in the merged config (a real `~/.config/imp/config.yaml`, or
    the dogfood's bootstrapped user config). Resetting on both sides makes the
    SGR-literal tests order-independent. (test_palette.py defines an identically
    named fixture; that local one overrides this for its own module — same
    effect.)"""
    from imp.cli import palette as pal
    pal.set_theme("ansi")
    yield
    pal.set_theme("ansi")


@pytest.fixture(autouse=True)
def _isolate_user_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirect XDG_CONFIG_HOME to an absent per-test dir so no test reads the real user config."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".xdg-isolated"))


@pytest.fixture(autouse=True)
def _isolate_session_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop ambient IMP_SESSION_SOCKET so make_store uses LocalStore, not a live daemon."""
    monkeypatch.delenv("IMP_SESSION_SOCKET", raising=False)


@pytest.fixture(autouse=True)
def _isolate_quest_tmp_root(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    """Give every test its own short, unique per-quest tmp/socket root.

    session_socket_path now derives from quest_id ALONE
    (<tmpdir>/imp-<uid>/<quest_id>/session.sock); the old path hashed the unique
    per-test control_root, which is what kept tests reusing hardcoded ids
    ("feat", "live-one", ...) from sharing one socket. Redirect the tmp root
    in-process (tempfile.tempdir) and for spawned session daemons (TMPDIR) to a
    short unique dir under the real system temp — short enough to keep the
    AF_UNIX socket within the 108-byte limit — restoring that per-test
    uniqueness. Under Part 1's identity-/tmp sandbox bind the redirected root is
    still under /tmp, so an in-sandbox client reaching the host socket path
    (injected via IMP_SESSION_SOCKET) still resolves it.

    Realize pytest's session basetemp FIRST: getbasetemp() derives (and caches)
    the basetemp from tempfile.gettempdir(), so patching tempfile.tempdir before
    it resolves would nest the basetemp inside our per-test root and the teardown
    rmtree would delete it — breaking every later test's tmp_path in serial runs
    (`make test XDIST=`, `-n0`, a bare multi-test `pytest <file>`). It is invisible
    under `-n auto` because xdist workers get a pre-resolved basetemp, so the
    verify gate would not catch it.
    """
    tmp_path_factory.getbasetemp()
    root = Path(tempfile.gettempdir()) / f"imp-t-{uuid.uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    monkeypatch.setenv("TMPDIR", str(root))
    yield
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def _sandbox_fallback_without_bwrap(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep run_quest-based tests green with OR without bwrap.

    run_quest builds a sandbox policy whose default posture (enabled, NOT
    allow_unsandboxed) HARD-FAILS with SandboxUnavailable when bwrap is absent.
    On a host WITH bwrap this fixture is a NO-OP, so those tests exercise the real
    sandbox. On a host WITHOUT bwrap it forces the executor's policy build to
    inactive so the run proceeds bare. Tests marked `real_sandbox_policy` (the
    unit tests asserting the raw three-way posture) opt out and see the real fn.
    """
    if request.node.get_closest_marker("real_sandbox_policy"):
        return
    from imp.engine.sandbox import sandbox_available
    if sandbox_available():
        return
    import imp.engine.executor as ex
    from imp.engine.sandbox import _inactive
    monkeypatch.setattr(
        ex, "_build_sandbox_policy",
        lambda quest, config, log: _inactive(note="test_fallback_no_bwrap"),
    )


@pytest.fixture(autouse=True)
def _no_inprocess_setpgrp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop in-process CLI tests from backgrounding the pytest runner.

    `_async_cmd_new`/`_async_cmd_resume` call `install_process_group()` ->
    `os.setpgrp()` as their first act. In production that's a harmless no-op:
    the shell already launched `imp` as the foreground process-group leader, so
    setpgrp leaves it foreground. But many tests invoke those entrypoints
    in-process (test_cli_interrupt, test_cli_view, test_cli_pause_reason,
    test_cli_resume_from, test_esc_pty), and `make` runs pytest inside *make's*
    foreground process group. There `os.setpgrp()` rips pytest into its own
    BACKGROUND group; pytest's next read/write to the controlling terminal then
    raises SIGTTIN/SIGTTOU and the kernel stops it (do_signal_stop) -- the
    intermittent `make test` hang. CI never hit it because redirected (non-tty)
    output can't raise SIGTTOU.

    Neutralize the syscall for the whole suite. No test asserts setpgrp actually
    runs (test_signals.py covers only the coordinator), so this is loss-free.
    """
    monkeypatch.setattr(os, "setpgrp", lambda: None, raising=False)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """An empty 'project' directory under tmp_path."""
    (tmp_path / ".imp").mkdir()
    return tmp_path


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
    )


def _init_repo(root: Path) -> None:
    """A git repo with one commit on branch `main` and `.imp/` gitignored."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")


def _seed_project(tmp_path: Path) -> None:
    """Produce the committed imp/ layout the worktree model requires.

    imp new needs >=1 commit with HEAD on a branch (worktree.check_preconditions)
    and reads the workflow + config from the control root's WORKING TREE
    (Quest.create copies imp/workflows/imp; the runtime reads the merged
    config from imp/project.yaml — the project layer of load_effective_config).
    Committing them is the clean choice. _init_repo already commits a README +
    .gitignore with `.imp/`, so .imp/ is durably ignored and `imp new` won't add
    an extra gitignore commit.
    """
    _init_repo(tmp_path)
    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(textwrap.dedent("""\
        ---
        name: writer
        description: A test agent that calls bash then complete_step.
        tools: [complete_step, bash]
        output:
          status: {type: enum, values: [Success, Error]}
        ---
        Run this shell command via bash: `sleep 30`. After bash returns
        (even if it was interrupted), call complete_step with status=Success.
        Do not call any other tool.
    """))
    (wf / "workflow.yaml").write_text(textwrap.dedent("""\
        name: t
        description: t
        defaults: {max_retries: 2, max_loops: 6, on_max_retries: {pause: true}}
        phases:
          - name: only
            steps:
              - {name: w, file: agents/writer.md, tier: low}
    """))
    (tmp_path / "imp" / "project.yaml").write_text(textwrap.dedent("""\
        default-workflow: imp
        default-base: main
        providers: [anthropic]
        tiers:
          low: {anthropic: claude-haiku-4-5-20251001}
          medium: {anthropic: claude-haiku-4-5-20251001}
          high: {anthropic: claude-haiku-4-5-20251001}
    """))
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "imp setup"],
        cwd=tmp_path, check=True,
    )


def _seed_control(root: Path) -> None:
    """A git repo with committed imp/project.yaml marker + imp/workflows/imp
    and a gitignored .imp/config.yaml local layer (read by load_effective_config)."""
    _init_repo(root)
    src = root / "imp" / "workflows" / "imp"
    (src / "agents").mkdir(parents=True)
    write_agent_md(
        src / "agents" / "stub.md",
        frontmatter=("name: stub\ntools: [complete_step]\n"
                     "output:\n  status: {type: enum, values: [Success, Error]}"),
    )
    (src / "workflow.yaml").write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n  - name: p\n    steps:\n      - {name: s, file: agents/stub.md}\n"
    )
    # Config layers: committed marker + gitignored local layer.
    (root / "imp" / "project.yaml").write_text(
        "default-workflow: imp\ndefault-base: main\n")
    (root / ".imp").mkdir(exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(
        "providers: [anthropic]\n"
        "tiers:\n"
        "  low: {anthropic: x}\n"
        "  medium: {anthropic: x}\n"
        "  high: {anthropic: x}\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup")


def write_agent_md(path: Path, frontmatter: str, body: str = "prompt body") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}\n")
    return path


def _archive_with_event(tmp_path: Path, quest_id: str, title: str, events_text: str) -> None:
    from imp.engine import archive

    events = tmp_path / f"{quest_id}.jsonl"
    events.write_text(events_text)
    quest_json = tmp_path / f"{quest_id}.quest.json"
    quest_json.write_text(json.dumps({
        "id": quest_id, "title": title, "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / f"{quest_id}.state.json"
    state_json.write_text(json.dumps({"id": quest_id, "status": "abandoned", "step": "spec"}))
    archive.archive_quest(
        tmp_path, quest_id, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)


@pytest.fixture
def provider_registry():
    """Reset the provider registry before AND after the test, yielding a thin
    register(...) wrapper over resolve.register_provider so tests keep their
    bespoke factories while the fixture owns teardown.

    Opt-in: name `provider_registry` as a test argument. Mirrors the
    reset-on-both-sides shape of test_subagent_spawn.py's autouse `_providers`
    fixture, but NON-autouse so only requesting tests are affected. The
    before-yield reset means a converted test no longer detects a registry
    polluted by a prior test (intentional parity with test_subagent_spawn.py);
    the after-yield reset still prevents leakage onto the next test.
    """
    from imp.providers import resolve

    resolve.reset_providers_for_tests()

    def register(name, env_check, factory):
        resolve.register_provider(name, env_check=env_check, factory=factory)

    yield register
    resolve.reset_providers_for_tests()
