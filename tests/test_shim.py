from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from imp import __version__
from imp.engine.shim import imp_shim_dir, write_imp_shim


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
    import imp
    outer_root = str(Path(imp.__file__).resolve().parent.parent)
    body = (write_imp_shim(tmp_path) / "imp").read_text()
    assert f"sys.path.insert(0, {outer_root!r})" in body  # outer imp's src dir, not worktree


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
