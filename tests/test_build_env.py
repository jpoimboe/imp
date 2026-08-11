"""The repo's build environment: the committed interpreter pin and the shape of
the Makefile's bootstrap targets."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_python_version_pins_the_exact_patch() -> None:
    # A minor-only pin ("3.14") resolves to the SYSTEM /usr/bin/python3.14 on a
    # box where its patch outranks the uv-managed build; only the exact patch
    # selects uv's own interpreter. And the pin only reaches other clones and
    # worktrees if git actually tracks it.
    assert (_REPO_ROOT / ".python-version").read_text() == "3.14.4\n"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".python-version"],
        cwd=_REPO_ROOT, capture_output=True, text=True,
    )
    assert tracked.returncode == 0, "git does not track .python-version"


def test_pyproject_declares_no_tool_uv_section() -> None:
    # python-preference = "only-managed" invalidates and recreates .venv on the
    # first run (uv >= 0.8.0) and pins no patch version at all; the exact-patch
    # .python-version is the whole mechanism.
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    assert "uv" not in data.get("tool", {})


def _make_n(target: str) -> subprocess.CompletedProcess[str]:
    """Expand a target's recipe without running it, so the assertions pin the
    real expanded command rather than Makefile source text."""
    # LC_ALL=C: make's diagnostics are gettext-translated, and one assertion
    # below reads make's own error text.
    return subprocess.run(
        ["make", "-n", target], cwd=_REPO_ROOT, capture_output=True, text=True,
        env={**os.environ, "LC_ALL": "C"},
    )


def test_sync_env_runs_uv_sync_with_all_extras() -> None:
    # `dev` is an extra, not a dependency-group, so --all-extras is what
    # installs the test tooling into this worktree's .venv.
    out = _make_n("sync-env")
    assert out.returncode == 0, out.stderr
    assert "uv sync --all-extras" in out.stdout
    # The export|pip-install form installed into whatever interpreter happened
    # to be active, which is what left the gate running on distro packages.
    assert "pip install" not in out.stdout


def test_install_target_is_gone() -> None:
    # A second bootstrap path re-resolves pyproject's loose ranges and installs
    # whatever is newest — exactly the drift `make depcheck` exists to catch.
    out = _make_n("install")
    assert out.returncode != 0
    assert "No rule to make target 'install'" in out.stderr


def test_pip_scope_is_gone() -> None:
    # PIP_SCOPE existed only to serve the deleted pip form.
    assert "PIP_SCOPE" not in (_REPO_ROOT / "Makefile").read_text()


# Every target that runs real work through $(PYTHON). `lockcheck` is absent on
# purpose (it only invokes $(UV)), as are `help`, `clean` and `sync-env`, which a
# fresh clone must be able to reach before any venv exists.
_GUARDED_TARGETS = (
    "test", "test-verbose", "test-live", "test-live-verbose", "test-live-quick",
    "test-live-codex-parallel", "test-all", "lint", "lint-fix", "format",
    "typecheck", "depcheck", "check", "build", "snap-ui", "snap-ui-light",
)


def _make(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the repo's Makefile from `cwd`; $(CURDIR)/.venv resolves under the caller's directory."""
    # Strip inherited make vars: PYTHON/XDIST would satisfy the child's ?= default under test;
    # MAKELEVEL/MAKEFLAGS make it print "Entering directory" to stdout, breaking the stdout assert.
    env = {k: v for k, v in os.environ.items()
           if k not in {"MAKEFLAGS", "MAKELEVEL", "MFLAGS", "PYTHON",
                        "XDIST", "LIVE_XDIST", "PYTEST", "PYTEST_ARGS"}}
    env["LC_ALL"] = "C"
    return subprocess.run(
        ["make", "-f", str(_REPO_ROOT / "Makefile"), *args],
        cwd=cwd, capture_output=True, text=True, env=env,
    )


@pytest.mark.parametrize("target", _GUARDED_TARGETS)
def test_gate_targets_refuse_to_run_without_a_venv(target: str, tmp_path: Path) -> None:
    # A scratch cwd IS the ".venv absent" case: $(PYTHON) is $(CURDIR)-relative,
    # so it resolves under tmp_path, where nothing ever created one.
    where = Path(os.path.realpath(tmp_path))
    out = _make(target, cwd=where)
    # GNU make maps ANY failed recipe to exit 2 — the guard's own shell exits 1.
    assert out.returncode == 2, f"{target}: {out.returncode}\n{out.stderr}"
    # Containment, not equality: make appends its own `*** [...] Error 1` line.
    assert (
        f"ERROR: no Python at {where}/.venv/bin/python\n"
        "create it with: make sync-env\n"
    ) in out.stderr
    assert out.stdout == "", f"{target} ran real work before the guard: {out.stdout!r}"


@pytest.mark.parametrize(("target", "extra"), [
    ("help", ()),
    ("clean", ()),
    ("sync-env", ("UV=true",)),
    ("lockcheck", ("UV=true",)),
])
def test_bootstrap_targets_still_work_without_a_venv(
        target: str, extra: tuple[str, ...], tmp_path: Path) -> None:
    # Guarding any of these would make a fresh clone unbootstrappable: sync-env is
    # what CREATES the venv. UV=true stubs the real uv — the scratch cwd has no
    # project to sync or lock.
    out = _make(target, *extra, cwd=Path(os.path.realpath(tmp_path)))
    assert out.returncode == 0, f"{target}: {out.stderr}"


def test_require_venv_accepts_a_path_resolved_interpreter(tmp_path: Path) -> None:
    # `command -v`, not `test -x`, so a PATH-resolved override such as
    # `make PYTHON=python3.13 test` still validates. Invoked on the guard target
    # itself: guarding a real target would spawn its work from inside the suite.
    out = _make("require-venv", "PYTHON=python3", cwd=Path(os.path.realpath(tmp_path)))
    assert out.returncode == 0, out.stderr


def test_xdist_probe_still_expands_behind_the_guard() -> None:
    # make expands a recursive $(shell) when the RECIPE expands, which is after
    # prerequisites are built — so fronting `test` with the guard must not leave
    # the XDIST probe empty and silently serialize the suite. PYTEST is stubbed
    # with printf, which echoes each expanded flag as its own bracketed word.
    pytest.importorskip("xdist")
    out = _make("test", f"PYTHON={sys.executable}", r"PYTEST=printf '[%s]\n'",
                cwd=_REPO_ROOT)
    assert out.returncode == 0, out.stderr
    assert "[-n]\n[auto]\n[--dist]\n[loadgroup]\n[-q]\n" in out.stdout


def test_the_xdist_probe_stays_recursively_expanded() -> None:
    # `?=` is recursive, so the probe's $(shell) runs when the RECIPE expands —
    # after require-venv has passed. A `:=` probe would instead run at parse
    # time, before the guard can abort, and on a tree with no .venv it yields
    # empty and silently serializes the suite. No behavioral assertion can catch
    # that swap here (the test above supplies PYTHON on the command line, which
    # makes `:=` expand correctly too), so pin the declaration itself.
    makefile = (_REPO_ROOT / "Makefile").read_text()
    assert re.search(r"^XDIST\s+\?=", makefile, re.M)
    assert re.search(r"^LIVE_XDIST\s+\?=", makefile, re.M)


def test_the_makefile_no_longer_pins_pythonpath() -> None:
    # One pin, in tests/conftest.py, covering `make test`, bare `pytest` and IDE
    # runners alike; the Makefile's copy only ever covered `make`.
    for path in ("Makefile", "tests/conftest.py"):
        assert "PYTEST_PYTHONPATH" not in (_REPO_ROOT / path).read_text(), path


def test_the_sandbox_comment_credits_conftest_for_the_pythonpath_prefix() -> None:
    # test_sandbox.py never spelled the variable's name, so a token check there
    # would pass vacuously; the stale claim is prose naming the Makefile as the
    # thing that sets PYTHONPATH, and after this change conftest is what does.
    text = (_REPO_ROOT / "tests" / "test_sandbox.py").read_text()
    assert "Makefile sets PYTHONPATH" not in text
    assert "tests/conftest.py pins PYTHONPATH for every pytest run" in text


def test_conftest_still_pins_this_worktree_on_the_subprocess_path(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # The surviving pin is the only thing keeping spawned `python -m imp` children
    # on THIS checkout rather than the global editable install; it must outlive the
    # Makefile's copy, and it must still PREPEND rather than clobber.
    from tests import conftest
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else")
    conftest._pin_subprocess_imp_to_working_tree()
    assert os.environ["PYTHONPATH"] == f"{_REPO_ROOT / 'src'}{os.pathsep}/somewhere/else"
