"""Shared pytest fixtures."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


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
    the real env through (their own skipif handles the "no auth" case);
    ordinary tests get the stub so module-level provider construction works.

    Live-API tests spawn subprocesses (or the claude_code provider's nested
    `claude`) that read ANTHROPIC_API_KEY from the real env. Stubbing it to
    "sk-test" would give the subprocess a fake key and cause "Invalid API key"
    / 401 errors. These tests follow a `_live` suffix convention
    (`*_live.py`, `*_live_<provider>.py`) and gate themselves with their own
    skipif, so exempt them from stubbing. The unrelated `test_live_*` UI
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
    in-process (e.g. `main(["ls"])`) calls `_apply_theme` -> `set_theme(...)`,
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
def _sandbox_fallback_without_bwrap(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep run_change-based tests green with OR without bwrap.

    run_change builds a sandbox policy whose default posture (enabled, NOT
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
        lambda change, config, log: _inactive(note="test_fallback_no_bwrap"),
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
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
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


def _make_imp_checkout(root: Path) -> None:
    pkg = root / "src" / "imp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")


def write_agent_md(path: Path, frontmatter: str, body: str = "prompt body") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}\n")
    return path


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
