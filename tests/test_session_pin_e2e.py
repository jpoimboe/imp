"""End to end: the imp a run executes is its snapshot's, not the checkout's — proven
against a copy of src/ that is rewritten after the pin, the way a merge to the base
branch rewrites the checkout under a live run."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import imp
from imp.engine.shim import write_imp_shim
from imp.remote.session.pin import pin_source_snapshot
from tests.async_wait import HANG_BACKSTOP

PINNED_VERSION = '__version__ = "9.9.9-pinned"\n'
MERGED_VERSION = '__version__ = "9.9.9-merged"\n'
MERGE_MARKER = "MERGED_AFTER_SNAPSHOT = True\n"


def _copy_checkout(dest: Path) -> Path:
    """A writable copy of the running src/ standing in for the control checkout, so the
    merge these tests simulate rewrites a copy and never this worktree."""
    ignore = shutil.ignore_patterns("__pycache__")
    running_src = Path(imp.__file__).resolve().parent.parent
    shutil.copytree(running_src / "imp", dest / "imp", ignore=ignore)
    shutil.copytree(running_src / "impty", dest / "impty", ignore=ignore)
    return dest


def _append(path: Path, text: str) -> None:
    with path.open("a") as fh:
        fh.write(text)


def _merge_into(checkout: Path) -> None:
    """Rewrite the checkout after the pin: a new version, plus a marker on the two
    modules a daemon reaches only mid-run — imp.api and the lazily imported
    impty.driver."""
    _append(checkout / "imp" / "__init__.py", MERGED_VERSION)
    _append(checkout / "imp" / "api.py", MERGE_MARKER)
    _append(checkout / "impty" / "driver.py", MERGE_MARKER)


def _child_env(checkout: Path) -> dict[str, str]:
    """The merged checkout replaces conftest's worktree-src PYTHONPATH pin: this is the
    imp a child would import if nothing put the snapshot ahead of it."""
    return {**os.environ, "PYTHONPATH": str(checkout)}


def test_the_shim_runs_snapshot_bytes_after_the_checkout_moves(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The in-sandbox `imp` is this shim, so a merge landing mid-run must not change the
    code it executes. The sentinel version exists only inside the snapshot, and the
    control subprocess shows the same child environment otherwise imports the merged
    checkout — so the shim's inserted path is what produced it."""
    checkout = _copy_checkout(tmp_path / "checkout" / "src")
    monkeypatch.setattr(imp, "__file__", str(checkout / "imp" / "__init__.py"))
    quest_dir = tmp_path / "quest"

    snapshot = pin_source_snapshot(quest_dir, tmp_path / "control")

    _append(snapshot / "imp" / "__init__.py", PINNED_VERSION)
    # The daemon writes its shim from inside the snapshot, after the re-exec.
    monkeypatch.setattr(imp, "__file__", str(snapshot / "imp" / "__init__.py"))
    shim = write_imp_shim(quest_dir) / "imp"
    _merge_into(checkout)

    assert f"sys.path.insert(0, {str(snapshot.resolve())!r})" in shim.read_text()
    # A copy, not a link: the sentinel stayed inside the snapshot.
    assert PINNED_VERSION not in (checkout / "imp" / "__init__.py").read_text()

    ran = subprocess.run(
        [str(shim), "--version"], capture_output=True, text=True, cwd=tmp_path,
        timeout=HANG_BACKSTOP, env=_child_env(checkout))
    control = subprocess.run(
        [sys.executable, "-c", "import imp; print(imp.__version__)"],
        capture_output=True, text=True, cwd=tmp_path,
        timeout=HANG_BACKSTOP, env=_child_env(checkout))

    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == "imp 9.9.9-pinned"
    # Control: this is what the shim would print if it pointed at the checkout.
    assert control.stdout.strip() == "9.9.9-merged", control
