from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import imp
from imp import __version__
from imp.engine.shim import imp_shim_dir, outer_imp_mismatch, write_imp_shim

# The src root the running imp was imported from, and the checkout that owns it.
_RUNNING_SRC = Path(imp.__file__).resolve().parent.parent
_CHECKOUT_ROOT = _RUNNING_SRC.parent


def test_imp_shim_dir_is_under_quest_dir(tmp_path: Path) -> None:
    assert imp_shim_dir(tmp_path) == tmp_path / "imp-shim"


def test_write_imp_shim_creates_executable_console_script(tmp_path: Path) -> None:
    d = write_imp_shim(tmp_path)
    assert d == tmp_path / "imp-shim"
    shim = d / "imp"
    assert shim.exists()
    assert shim.stat().st_mode & 0o777 == 0o755            # exact 0o755 mode
    body = shim.read_text()
    assert body.startswith(f"#!{sys.executable}\n")        # outer-interpreter shebang
    assert "sys.path.insert(0, " in body                   # outer-root at sys.path[0]
    assert "from imp.cli.main import main" in body         # console-script form
    assert "-m imp" not in body  # never the -m form (namespace-shadow guard)
    assert body.index("sys.path.insert") < body.index("from imp.cli.main")  # order guard


def test_write_imp_shim_embeds_outer_src_root(tmp_path: Path) -> None:
    body = (write_imp_shim(tmp_path) / "imp").read_text()
    assert f"sys.path.insert(0, {str(_RUNNING_SRC)!r})" in body  # outer src dir, not worktree


def _run_shim(shim: Path, *args: str, cwd: Path,
              env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(shim), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_shim_execution_runs_outer_imp(tmp_path: Path) -> None:
    """Executing the shim file invokes the running imp's CLI (baseline)."""
    shim = write_imp_shim(tmp_path) / "imp"
    result = _run_shim(shim, "--version", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"imp {__version__}"


def test_shim_resolves_outer_imp_over_pythonpath_shadow(tmp_path: Path) -> None:
    """With an injected PYTHONPATH=<other>/imp shadow (as the agent env carries
    PYTHONPATH=<worktree>/src), the shim still loads the OUTER imp — because it
    inserts the outer src root at sys.path[0] before importing imp. If the shadow
    won, `from imp.cli.main import main` would raise (the fake has no `cli`
    subpackage) and the shim would exit non-zero. Falsifiable: reverting the shim
    to import imp before the sys.path.insert, or dropping the insert, flips this
    to a non-zero exit with a ModuleNotFoundError traceback."""
    fake_root = tmp_path / "fake"
    (fake_root / "imp").mkdir(parents=True)
    (fake_root / "imp" / "__init__.py").write_text('__version__ = "SHADOW"\n')

    shim = write_imp_shim(tmp_path) / "imp"
    env = {**os.environ, "PYTHONPATH": str(fake_root)}
    result = _run_shim(shim, "--version", cwd=tmp_path, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"imp {__version__}"
    assert "SHADOW" not in result.stdout


def test_shim_survives_cwd_imp_namespace_trap(tmp_path: Path) -> None:
    """Run from a cwd containing an `imp/` directory that lacks __init__.py — a
    PEP 420 namespace-package trap. Because the shim is a script FILE, sys.path[0]
    is the shim's own dir, never the cwd, so the empty `imp/` cannot shadow the
    real package. Falsifiable: a `python -m imp` / `python -c` shim would put the
    cwd first on sys.path, the empty `imp/` would win as a namespace package, and
    `from imp.cli.main import main` would raise (imp.cli missing) → non-zero exit."""
    trap_cwd = tmp_path / "trap"
    (trap_cwd / "imp").mkdir(parents=True)   # namespace-package trap: no __init__.py

    shim = write_imp_shim(tmp_path) / "imp"
    result = _run_shim(shim, "--version", cwd=trap_cwd)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"imp {__version__}"


def test_command_v_imp_resolves_to_shim_on_path(tmp_path: Path) -> None:
    """With the shim dir on PATH, `command -v imp` resolves to the shim file, so
    an agent shell can invoke `imp`. Falsifiable: if the shim were not written as
    an executable file named `imp`, command -v would resolve elsewhere or fail."""
    shim_dir = write_imp_shim(tmp_path)
    env = {**os.environ, "PATH": f"{shim_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"}
    result = subprocess.run(
        ["sh", "-c", "command -v imp"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(shim_dir / "imp")


def test_shim_dir_precedes_preexisting_imp_on_path(tmp_path: Path) -> None:
    """When the shim dir precedes another PATH dir that also contains an `imp`
    executable, the shim's imp is chosen (prepend wins). Falsifiable: appending
    the shim dir instead of prepending would resolve to the decoy `imp` first."""
    shim_dir = write_imp_shim(tmp_path)
    decoy_dir = tmp_path / "decoy"
    decoy_dir.mkdir()
    decoy = decoy_dir / "imp"
    decoy.write_text("#!/bin/sh\necho DECOY\n")
    decoy.chmod(0o755)

    env = {**os.environ,
           "PATH": f"{shim_dir}{os.pathsep}{decoy_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"}
    result = subprocess.run(
        ["sh", "-c", "command -v imp"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(shim_dir / "imp")


def test_outer_imp_mismatch_silent_when_running_from_this_checkout() -> None:
    """The suite runs against the checkout's own src (pyproject pythonpath=["src"]),
    so the real checkout root must produce no message. The name guard keeps this
    from passing vacuously if imp were ever imported from site-packages instead."""
    assert _RUNNING_SRC.name == "src"
    assert outer_imp_mismatch(_CHECKOUT_ROOT) is None


def test_outer_imp_mismatch_silent_when_project_is_not_an_imp_checkout(
        tmp_path: Path) -> None:
    assert outer_imp_mismatch(tmp_path) is None          # no src/ at all
    (tmp_path / "src").mkdir()
    assert outer_imp_mismatch(tmp_path) is None          # src/, but no imp package


def test_outer_imp_mismatch_names_both_roots_and_the_fix(tmp_path: Path) -> None:
    (tmp_path / "src" / "imp").mkdir(parents=True)
    (tmp_path / "src" / "imp" / "__init__.py").write_text("")

    message = outer_imp_mismatch(tmp_path)

    assert message is not None
    assert message.startswith("outer imp mismatch: ")
    assert str(_RUNNING_SRC) in message
    assert str((tmp_path / "src").resolve()) in message
    assert f"python3 -m pip install -e {tmp_path}" in message


def test_outer_imp_mismatch_silent_through_a_symlinked_checkout(tmp_path: Path) -> None:
    """A checkout reached by symlink is the same tree: both sides are resolved, so
    the symlinked path must not read as a mismatch."""
    link = tmp_path / "checkout-link"
    link.symlink_to(_CHECKOUT_ROOT, target_is_directory=True)

    assert outer_imp_mismatch(link) is None


def _fake_snapshot(root: Path, *, source_root: Path) -> Path:
    """A directory shaped like a published pin: an `imp` package plus provenance."""
    snapshot = root / "pinned-src" / "tok"
    (snapshot / "imp").mkdir(parents=True)
    (snapshot / "imp" / "__init__.py").write_text("")
    (snapshot / "source-root").write_text(f"{source_root}\n")
    return snapshot


def test_outer_imp_mismatch_silent_for_a_snapshot_of_this_checkout(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pinned run executes from <quest home>/pinned-src/<token>, which is never the
    checkout's own src — so without provenance every imp-on-imp run would warn. The
    marker names the tree the snapshot came from; that is what must be compared."""
    snapshot = _fake_snapshot(tmp_path, source_root=_RUNNING_SRC)
    monkeypatch.setattr(imp, "__file__", str(snapshot / "imp" / "__init__.py"))

    assert outer_imp_mismatch(_CHECKOUT_ROOT) is None


def test_outer_imp_mismatch_names_the_source_root_a_snapshot_came_from(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A snapshot taken from an unrelated install still warns — and names that install,
    the operator error the warning exists for, not the snapshot it was copied into."""
    checkout = tmp_path / "checkout"
    (checkout / "src" / "imp").mkdir(parents=True)
    (checkout / "src" / "imp" / "__init__.py").write_text("")
    foreign_src = tmp_path / "site-packages"
    (foreign_src / "imp").mkdir(parents=True)
    snapshot = _fake_snapshot(tmp_path, source_root=foreign_src)
    monkeypatch.setattr(imp, "__file__", str(snapshot / "imp" / "__init__.py"))

    message = outer_imp_mismatch(checkout)

    assert message is not None
    assert message.startswith("outer imp mismatch: ")
    assert str(foreign_src) in message
    assert str(snapshot) not in message
    assert str((checkout / "src").resolve()) in message
