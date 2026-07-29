"""Shared pytest fixtures."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from imp.paths import project_state_dir

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
# The session-daemon/socket test family (test_session_daemon.py,
# test_session_run.py, test_session_attach.py) drives a faked run_fn over a real
# AF_UNIX socket — no provider stream. Under `-n auto` oversubscription pure
# scheduling starvation can push a structurally-sound test past the global
# timeout = 60 (signal method, pyproject.toml) and SIGALRM kills it. Grant this
# family the same generous liveness ceiling the live/impty families get; at 2x
# the 120s inner HANG_BACKSTOP the inner backstop trips first on a genuine
# deadlock and the outer cap only ever catches a true hang. Scoped to these
# three evidence-based files — other test_session_*.py keep the 60s cap.
_SESSION_SOCKET_TEST_TIMEOUT = 240
_SESSION_SOCKET_TEST_FILES = frozenset({
    "test_session_daemon.py", "test_session_run.py", "test_session_attach.py",
})


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


# The basetemp we rooted this run at, populated ONLY when we own it (never a
# user-supplied --basetemp, never an xdist worker). pytest_unconfigure keys the
# teardown reap on this being set, so the guard is structural, not re-derived.
_OWNED_BASETEMP: str | None = None


def pytest_configure(config: pytest.Config) -> None:
    # Numerous tests hand-bind AF_UNIX sockets DIRECTLY under tmp_path
    # (str(tmp_path / "s.sock") / "rc.sock" in test_session_client.py,
    # test_daemon_southhub_*.py, test_daemon_connector_*.py, test_mutation_proxy.py,
    # test_daemon_discover_socket.py's socket_is_live tests, etc.), so tmp_path must
    # stay under the 107-byte AF_UNIX sun_path limit under -n auto. (imp's two
    # PRODUCTION sockets — the per-quest session socket and the remote-control
    # socket — have been relocated to /tmp/imp-<uid>/ and no longer depend on this
    # pin; the test-local binds still do.) tmp_path descends from basetemp, so
    # root the whole run at a short FIXED basetemp `/tmp/ip<6hex>`, independent of
    # tempfile.gettempdir()/TMPDIR length: inside imp's bwrap sandbox TMPDIR is a
    # long per-quest scratch (/tmp/imp-<uid>/<questid>) that would otherwise blow
    # those socket paths over the limit. `/tmp` is the shortest guaranteed-writable
    # root (identity-bound rw in the sandbox, src/imp/engine/sandbox.py); the
    # random 6-hex suffix keeps concurrent suite runs on the shared host /tmp
    # unique. Skip when the user set --basetemp explicitly, and on xdist workers
    # (they inherit the controller's basetemp via xdist). pytest_unconfigure reaps
    # the dir we created on clean session end — it is no longer nested under the
    # engine-reaped quest scratch, so nothing else cleans it.
    global _OWNED_BASETEMP
    if not config.option.basetemp and "PYTEST_XDIST_WORKER" not in os.environ:
        basetemp = os.path.join("/tmp", f"ip{uuid.uuid4().hex[:6]}")
        config.option.basetemp = basetemp
        _OWNED_BASETEMP = basetemp


def pytest_unconfigure(config: pytest.Config) -> None:
    # Reap only the basetemp we created; a populated _OWNED_BASETEMP already
    # excludes a user-supplied --basetemp and xdist workers (which never set it),
    # so workers never race the controller's removal.
    if _OWNED_BASETEMP is not None:
        shutil.rmtree(_OWNED_BASETEMP, ignore_errors=True)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item],
) -> None:
    """Two jobs at collection: (1) raise the per-test timeout for the slow
    liveness families — live-API `*_live.py` files (see _LIVE_TEST_TIMEOUT),
    impty PTY e2e `tests/impty/test_e2e_*.py` files (see _IMPTY_E2E_TIMEOUT), and
    the session-daemon/socket family (see _SESSION_SOCKET_TEST_FILES /
    _SESSION_SOCKET_TEST_TIMEOUT) — each keeping its own explicit `timeout`
    marker if it declares one (e.g. the resume tests that pick 240); and (2) gate
    every `@pytest.mark.live(<provider>)` test behind --run-live (see
    tests/_live.py's live_skip_reason)."""
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
        is_session_socket = name in _SESSION_SOCKET_TEST_FILES
        if is_session_socket and item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(_SESSION_SOCKET_TEST_TIMEOUT))
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
    `theme:` lives in the merged config (a real `~/.imp/config.yaml`, or
    the dogfood's bootstrapped user config). Resetting on both sides makes the
    SGR-literal tests order-independent. (test_palette.py defines an identically
    named fixture; that local one overrides this for its own module — same
    effect.)"""
    from imp.cli import palette as pal
    pal.set_theme("ansi")
    yield
    pal.set_theme("ansi")


@pytest.fixture(autouse=True)
def _isolate_global_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point IMP_HOME at an absent per-test dir so no test reads the real global
    config. XDG_CONFIG_HOME stays set for one reason only — it is the startup
    migration's legacy source, and a test that shells out to a real `imp`
    inherits both."""
    monkeypatch.setenv("IMP_HOME", str(tmp_path / ".imp-isolated"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".xdg-isolated"))


@pytest.fixture(autouse=True)
def _isolate_session_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop ambient IMP_SESSION_SOCKET so make_store uses LocalStore, not a live daemon."""
    monkeypatch.delenv("IMP_SESSION_SOCKET", raising=False)


def clear_state_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop an inherited IMP_STATE_DIR — imp builds imp, so pytest runs as an agent
    subprocess with the var injected, and every test would then share one state
    directory instead of resolving under its own tmp_path."""
    monkeypatch.delenv("IMP_STATE_DIR", raising=False)


@pytest.fixture(autouse=True)
def _isolate_state_dir(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    clear_state_dir(monkeypatch)
    yield
    # The startup migration pins the pre-migration path by writing os.environ
    # directly; monkeypatch never saw that set and cannot undo it. Drop it here so
    # it can't leak into the next test in this worker. monkeypatch's own undo runs
    # after this teardown and still restores a genuinely inherited value.
    os.environ.pop("IMP_STATE_DIR", None)


@pytest.fixture(autouse=True)
def _isolate_quest_tmp_root(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    """Give every test its own short, unique per-quest tmp/socket root.

    The session socket now lives as a sibling of Quest.tmp_dir under
    tempfile.gettempdir() (<tmpdir>/imp-<uid>/<id>.sock), so redirecting
    tempfile.tempdir + TMPDIR gives Quest.tmp_dir, spawned session daemons, and
    the socket a single short unique per-test root.

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
    # real-disk /var/tmp twin (overlayfs refuses tmpfs upperdir); unique per test
    var_root = Path("/var/tmp") / f"imp-t-{uuid.uuid4().hex[:12]}"
    var_root.mkdir(mode=0o700)
    monkeypatch.setenv("IMP_QUEST_VAR_TMP", str(var_root))
    yield
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(var_root, ignore_errors=True)


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
    Tests marked `bare_sandbox` force the policy inactive even on a bwrap host, so
    a gate-routing test never flakes on a transient bwrap spawn failure.
    """
    if request.node.get_closest_marker("real_sandbox_policy"):
        return
    from imp.engine.sandbox import _inactive, sandbox_available
    force_bare = request.node.get_closest_marker("bare_sandbox") is not None
    if not force_bare and sandbox_available():
        return
    import imp.engine.executor as ex
    note = "bare_sandbox_marker" if force_bare else "test_fallback_no_bwrap"
    monkeypatch.setattr(
        ex, "_build_sandbox_policy",
        lambda quest, config, log: _inactive(note=note),
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
    return tmp_path


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
    )


def _stop_at_rebase_edit(root: Path) -> dict[str, str]:
    """Park `root` mid `rebase -i HEAD~2` at an edit step; returns env for `--continue`."""
    edit_first = (
        f"{shlex.quote(sys.executable)} -c "
        "\"import pathlib, re, sys; p = pathlib.Path(sys.argv[1]); "
        "p.write_text(re.sub(r'^(?:pick|p)\\b', 'edit', p.read_text(), count=1, flags=re.M))\""
    )
    env = {**os.environ, "GIT_SEQUENCE_EDITOR": edit_first, "GIT_EDITOR": "true"}
    started = subprocess.run(
        ["git", "rebase", "-i", "HEAD~2"], cwd=root,
        env=env, capture_output=True, text=True)
    assert started.returncode == 0, started.stderr
    assert (root / ".git" / "rebase-merge").is_dir()
    return env


def _assert_outside_a_git_work_tree(path: Path) -> None:
    """A startup migration's no-op guarantee rests on tmp_path not being a work
    tree; a --basetemp inside the checkout would silently make the step real."""
    from imp.engine.worktree import PreconditionError, find_control_root

    with pytest.raises(PreconditionError):
        find_control_root(path)


def _init_repo(root: Path) -> None:
    """A git repo with one commit on branch `main`, ignoring imp's state dirs."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n.imp-isolated/\n")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")


def _branch_with_commit(root: Path, name: str) -> str:
    """Create `name` with an extra commit, restore the checkout to main, return its tip sha."""
    _git(root, "checkout", "-q", "-b", name)
    (root / f"{name}.txt").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", f"seed {name}")
    tip = _git(root, "rev-parse", name).stdout.strip()
    _git(root, "checkout", "-q", "main")
    return tip


def _seed_project(tmp_path: Path) -> None:
    """Produce the committed imp/ layout the worktree model requires.

    imp new needs >=1 commit (worktree.check_preconditions)
    and reads the workflow + config from the control root's WORKING TREE
    (Quest.create copies imp/workflows/imp; the settings layers are the control
    root's imp/config.yaml + its state dir's config.yaml — a quest worktree is never read).
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
    (tmp_path / "imp" / "config.yaml").write_text(textwrap.dedent("""\
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
    """A git repo with committed imp/config.yaml marker + imp/workflows/imp
    and an out-of-tree config.yaml local layer (read by load_effective_config)."""
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
    # Config layers: committed marker + out-of-tree local layer.
    (root / "imp" / "config.yaml").write_text(
        "default-workflow: imp\ndefault-base: main\n")
    (project_state_dir(root) / "config.yaml").write_text(
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


def _spin_hold_lock(lock_path: str, release_flag: str, ready_flag: str) -> None:
    from imp.engine.state import ProjectLock
    with ProjectLock(Path(lock_path)).acquire():
        Path(ready_flag).touch()
        while not Path(release_flag).exists():
            time.sleep(0.05)


@contextmanager
def held_lock(lock_path: Path) -> Generator[None, None, None]:
    """Spawn a subprocess holding ProjectLock(lock_path) for the with-block, so
    code under test sees a live daemon's lock. Polls for the real 'acquired'
    signal (generous 10s inner budget) rather than sleeping a fixed interval."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    release_flag = lock_path.parent / f"{lock_path.name}.release"
    ready_flag = lock_path.parent / f"{lock_path.name}.ready"
    proc = mp.get_context("spawn").Process(
        target=_spin_hold_lock,
        args=(str(lock_path), str(release_flag), str(ready_flag)))
    proc.start()
    try:
        deadline = time.monotonic() + 10.0
        while not ready_flag.exists():
            if time.monotonic() > deadline:
                raise AssertionError("lock holder never signaled ready within 10s")
            time.sleep(0.05)
        yield
    finally:
        release_flag.touch()
        proc.join(timeout=5)
        assert not proc.is_alive()  # surface a wedged holder instead of leaking a spawn
