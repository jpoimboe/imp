"""`scripts/check_deps.py` is the only gate that inspects the interpreter the
suite actually runs on, so its detection logic needs its own coverage: a green
`make lockcheck` says nothing about what is installed here."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    """Import scripts/check_deps.py by path — it is a script, not a package."""
    spec = importlib.util.spec_from_file_location(
        "check_deps", _REPO_ROOT / "scripts" / "check_deps.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_make_check_runs_the_dependency_preflight() -> None:
    """depcheck must stay wired into the gate, and run BEFORE the slow targets
    so a drifted interpreter fails in seconds rather than mid-suite."""
    out = subprocess.run(
        ["make", "-n", "check"],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    assert "check_deps.py" in out


@pytest.mark.parametrize(("raw", "want"), [
    ("prompt_toolkit", "prompt-toolkit"),
    ("types-PyYAML", "types-pyyaml"),
    ("ruamel.yaml", "ruamel-yaml"),
    ("anyio", "anyio"),
])
def test_canon_normalizes_the_three_spellings(raw: str, want: str) -> None:
    # pyproject, uv.lock, and installed metadata spell these differently; a
    # missed normalization silently skips the package instead of checking it.
    assert _load().canon(raw) == want


def test_locked_versions_reads_the_real_lockfile() -> None:
    locked = _load().locked_versions(_REPO_ROOT / "uv.lock")
    assert locked, "uv.lock parsed to nothing"
    # Every key must already be canonical, or lookups against it silently miss.
    mod = _load()
    assert all(k == mod.canon(k) for k in locked)


def test_stale_version_fails_the_gate(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A package installed at a version uv.lock does not pin is the drift that
    a loose pyproject range lets through (`prompt_toolkit>=3.0,<4.0` accepting a
    3.0.53 the lockfile never resolved to). It must fail, not warn."""
    mod = _load()
    monkeypatch.delenv("ALLOW_DEP_DRIFT", raising=False)
    monkeypatch.setattr(mod, "_installed", lambda name: "0.0.0-drifted")

    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "uv.lock does not pin" in err
    assert "installed 0.0.0-drifted" in err
    assert "make sync-env" in err


def test_allow_dep_drift_downgrades_stale_to_a_warning(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Escape hatch for deliberately testing a bump before `uv lock --upgrade`."""
    mod = _load()
    monkeypatch.setenv("ALLOW_DEP_DRIFT", "1")
    monkeypatch.setattr(mod, "_installed", lambda name: "0.0.0-drifted")

    assert mod.main() == 0
    assert "warning" in capsys.readouterr().err


def test_missing_package_is_fatal_even_under_allow_dep_drift(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The escape hatch covers version skew only — the suite genuinely cannot
    run against a missing package, so that stays fatal in every mode."""
    mod = _load()
    monkeypatch.setenv("ALLOW_DEP_DRIFT", "1")
    monkeypatch.setattr(mod, "_installed", lambda name: None)

    assert mod.main() == 1
    assert "not installed in this interpreter" in capsys.readouterr().err


def test_clean_interpreter_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exact-match installs must return 0 — otherwise the gate cries wolf."""
    mod = _load()
    locked = mod.locked_versions(_REPO_ROOT / "uv.lock")
    monkeypatch.setattr(mod, "_installed", lambda name: locked.get(mod.canon(name), "1.0"))
    assert mod.main() == 0
