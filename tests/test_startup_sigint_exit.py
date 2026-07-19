"""Non-live regression: SIGINT during imp's startup window exits 130, never -2.

Guards the outer entry-point backstop in src/imp/__main__.py. When SIGINT lands
before imp's absorbing asyncio loop signal handler installs (during the heavy
`imp.cli.main` import + the anyio bootstrap), CPython restores SIG_DFL and, absent
the guard, the process is reaped killed-by-signal (wait status -2) instead of
exiting 130. This spawns `imp new` in its own session and delivers SIGINT ~0.1s in
(inside that pre-handler window), asserting a clean 130 and specifically never -2.
It fails (observes -2) against the pre-fix __main__.py and passes against the
guarded one. No API keys are needed: the process is interrupted during startup,
before any LLM call.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from tests.conftest import _WORKTREE_SRC, _seed_project


def test_startup_sigint_exits_130_never_minus_2(tmp_path: Path) -> None:
    _seed_project(tmp_path)
    env = os.environ.copy()
    # A stub key defends against any early credential presence-check aborting
    # (rc=1) before the interrupt window; we interrupt during startup, so no LLM
    # call is ever made and the value is never validated.
    env["ANTHROPIC_API_KEY"] = "sk-ant-startup-sigint-stub"
    _pypath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _WORKTREE_SRC + (os.pathsep + _pypath if _pypath else "")

    proc = subprocess.Popen(
        [sys.executable, "-m", "imp", "new", "startup-sigint"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Own session/process group so os.killpg targets the imp process directly.
        start_new_session=True,
    )
    try:
        # ~0.1s is inside the pre-handler startup window (the spike's delay sweep:
        # SIGINT at 0.0-0.35s -> -2; the loop handler installs ~0.5s in, and
        # `imp new` does synchronous startup + quest creation before anyio.run),
        # so pre-fix this reliably reproduces the -2 reaping. Landing slightly
        # later still exits 130 once the handler absorbs SIGINT, so the assertion
        # holds either way and -2 is never observed post-fix.
        time.sleep(0.1)
        os.killpg(proc.pid, signal.SIGINT)
        rc = proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    assert rc != -2, (
        "process reaped killed-by-SIGINT (wait status -2): the startup-window "
        "KeyboardInterrupt was unhandled instead of exiting 130"
    )
    assert rc == 130, f"expected clean SIGINT exit 130, got {rc}"


def test_console_script_startup_sigint_exits_130_never_minus_2(tmp_path: Path) -> None:
    # Exercises the installed console-script entry-point *import* path
    # (`from imp.__main__ import main; sys.exit(main())`), distinct from runpy's
    # `-m imp` path. Hermetic: synthesizes the setuptools/hatch-style stub rather
    # than depending on a pipx/editable-installed `imp` binary.
    _seed_project(tmp_path)
    env = os.environ.copy()
    env["ANTHROPIC_API_KEY"] = "sk-ant-startup-sigint-stub"
    _pypath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _WORKTREE_SRC + (os.pathsep + _pypath if _pypath else "")

    stub = tmp_path / "imp_console_stub.py"
    stub.write_text(
        "import sys\nfrom imp.__main__ import main\nsys.exit(main())\n",
        encoding="utf-8",
    )

    proc = subprocess.Popen(
        [sys.executable, str(stub), "new", "startup-sigint"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        time.sleep(0.1)
        os.killpg(proc.pid, signal.SIGINT)
        rc = proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    assert rc != -2, (
        "console-script process reaped killed-by-SIGINT (wait status -2): the "
        "startup-window KeyboardInterrupt was unhandled instead of exiting 130"
    )
    assert rc == 130, f"expected clean SIGINT exit 130, got {rc}"
