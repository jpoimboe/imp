"""Non-live regression: SIGINT during imp's startup window exits 130, never -2.

Guards the outer entry-point backstop in src/imp/__main__.py: a module-level
line-14 SIGINT guard (_exit_130_on_sigint -> SystemExit(130)) installed at import,
before the heavy imp.cli.main import + anyio bootstrap where CPython would
otherwise leave SIGINT able to reap the process killed-by-signal (wait status -2)
instead of exiting 130.

Two complementary, race-free strategies:

- test_startup_sigint_guard_installed_converts_to_130 is the falsifiability
  anchor: a child imports imp.__main__, asserts the installed SIGINT handler IS
  _exit_130_on_sigint, then synchronously raise_signal(SIGINT) and must exit 130.
  Remove the line-14 guard and this fails deterministically (handler is CPython's
  default_int_handler; the child asserts and exits non-130).

- test_startup_sigint_exits_130_never_minus_2 and its console-script twin prove the
  clean-130 exit end-to-end through the two real entry framings (runpy `-m imp` and
  the `from imp.__main__ import main; sys.exit(main())` console-script stub). Each
  child is pinned *inside* the guarded window deterministically -- a tmp_path
  sitecustomize.py installs a sys.meta_path finder that blocks forever on the
  imp.cli.main import (after line 14 installs the guard, before imp's absorbing
  asyncio handler is reached) and touches a marker file the instant it blocks, so
  the parent delivers SIGINT with no wall-clock dependence and asserts a clean 130,
  never -2. No API keys, quest seeding, or argument parsing are involved.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from tests.conftest import _WORKTREE_SRC

_SITECUSTOMIZE = '''\
import os
import sys
import time


class _BlockImpCliMain:
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "imp.cli.main":
            return None
        marker = os.environ["IMP_SIGINT_MARKER"]
        with open(marker, "w") as fh:
            fh.write("blocked")
        while True:
            time.sleep(0.05)


sys.meta_path.insert(0, _BlockImpCliMain())
'''


def _run_startup_sigint(tmp_path: Path, argv: list[str]) -> int:
    """Spawn `argv`, wait until it blocks in the guarded window, SIGINT it, reap.

    The child imports imp.__main__ (installing the line-14 SIGINT guard), then
    blocks at the imp.cli.main import via the sitecustomize finder, touching the
    marker file. The marker is a durable, wall-clock-free signal that SIGINT will
    land in the guarded window (guard installed, absorbing handler unreached).
    """
    marker = tmp_path / "sigint.marker"
    (tmp_path / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")

    env = os.environ.copy()
    env["IMP_SIGINT_MARKER"] = str(marker)
    env["PYTHONPATH"] = _WORKTREE_SRC + os.pathsep + str(tmp_path)

    proc = subprocess.Popen(
        argv,
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Own session/process group so os.killpg targets the child directly.
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 30.0
        while not marker.exists():
            if proc.poll() is not None:
                raise AssertionError(
                    f"child exited during startup (rc={proc.returncode}) before "
                    "the guarded-window marker appeared"
                )
            if time.monotonic() > deadline:
                raise AssertionError("timed out waiting for guarded-window marker")
            time.sleep(0.01)
        os.killpg(proc.pid, signal.SIGINT)
        return proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_startup_sigint_exits_130_never_minus_2(tmp_path: Path) -> None:
    # runpy entry framing: `python -m imp`. The child blocks at the imp.cli.main
    # import before argparse, so no subcommand is needed.
    rc = _run_startup_sigint(tmp_path, [sys.executable, "-m", "imp"])
    assert rc != -2, (
        "process reaped killed-by-SIGINT (wait status -2): the startup-window "
        "KeyboardInterrupt was unhandled instead of exiting 130"
    )
    assert rc == 130, f"expected clean SIGINT exit 130, got {rc}"


def test_console_script_startup_sigint_exits_130_never_minus_2(tmp_path: Path) -> None:
    # Console-script entry framing: the installed-binary import path
    # (`from imp.__main__ import main; sys.exit(main())`), synthesized as a stub so
    # the test does not depend on a pipx/editable-installed `imp` binary.
    stub = tmp_path / "imp_console_stub.py"
    stub.write_text(
        "import sys\nfrom imp.__main__ import main\nsys.exit(main())\n",
        encoding="utf-8",
    )
    rc = _run_startup_sigint(tmp_path, [sys.executable, str(stub)])
    assert rc != -2, (
        "console-script process reaped killed-by-SIGINT (wait status -2): the "
        "startup-window KeyboardInterrupt was unhandled instead of exiting 130"
    )
    assert rc == 130, f"expected clean SIGINT exit 130, got {rc}"


def test_startup_sigint_guard_installed_converts_to_130(tmp_path: Path) -> None:
    # Deterministic falsifiability anchor (no wall-clock race): importing
    # imp.__main__ must install the module-level SIGINT guard so a startup-window
    # SIGINT exits 130 instead of a killed-by-signal reaping. The child asserts the
    # installed handler IS _exit_130_on_sigint (not CPython's default_int_handler),
    # then raises SIGINT to exercise it. Remove src/imp/__main__.py's
    # line-14 guard and this fails: the handler is default_int_handler, the child's
    # assert trips (rc != 130). raise_signal queues the signal into CPython's eval
    # loop; the handler is guaranteed to fire at the next bytecode boundary (before
    # time.sleep), so there is no timing dependence and the sleep is only a bounded
    # safety net if the handler never fires.
    prog = (
        "import signal\n"
        "import time\n"
        "import imp.__main__ as m\n"
        "assert signal.getsignal(signal.SIGINT) is m._exit_130_on_sigint, "
        "signal.getsignal(signal.SIGINT)\n"
        "signal.raise_signal(signal.SIGINT)\n"
        "time.sleep(5)\n"
        "raise SystemExit(99)\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = _WORKTREE_SRC
    proc = subprocess.run(
        [sys.executable, "-c", prog],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 130, (
        "importing imp.__main__ did not install the SIGINT guard that converts a "
        f"startup SIGINT to a clean 130; got rc={proc.returncode}, "
        f"stderr={proc.stderr.decode(errors='replace')!r}"
    )
