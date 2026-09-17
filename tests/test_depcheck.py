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


def _clean_interpreter(mod: ModuleType, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Pretend every locked package is installed at exactly its pin, so a test can
    isolate one failure class from whatever this interpreter actually has."""
    locked = mod.locked_versions(_REPO_ROOT / "uv.lock")
    monkeypatch.setattr(mod, "_installed", lambda name: locked.get(mod.canon(name), "1.0"))
    return locked


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
    """A package installed at a version uv.lock does not pin is the drift that a
    loose pyproject range lets through (a `foo>=1.0,<2.0` accepting a 1.9 the
    lockfile never resolved to). It must fail, not warn."""
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
    _clean_interpreter(mod, monkeypatch)
    assert mod.main() == 0


def test_declared_floors_parses_every_shape_pyproject_uses(tmp_path: Path) -> None:
    # Capped range, no bound at all, dotted name, extras bracket, a dev-extra entry,
    # and both marker directions — a `>=` marker is the shape that misparses as the
    # floor. Miss any of them and the gate skips or misjudges the package.
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\n'
        'name = "x"\n'
        'dependencies = [\n'
        '  "anyio>=4.15.1,<5.0",\n'
        '  "pillow",\n'
        '  "ruamel.yaml>=0.19.1",\n'
        '  "uvicorn[standard]>=0.40.0",\n'
        "  \"tomli>=2.0 ; python_version < '3.11'\",\n"
        "  \"exceptiongroup ; python_version >= '3.11'\",\n"
        ']\n'
        '[project.optional-dependencies]\n'
        'dev = ["types-PyYAML>=6.0.12.20260906"]\n'
    )
    assert _load().declared_floors(pyproject) == {
        "anyio": "4.15.1",
        "pillow": None,
        "ruamel-yaml": "0.19.1",
        "uvicorn": "0.40.0",
        "tomli": "2.0",
        "exceptiongroup": None,
        "types-pyyaml": "6.0.12.20260906",
    }


def test_floor_that_is_not_the_locked_version_fails_the_gate(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A declared floor below the pin is a published claim about an untested
    combination — `uv lock --upgrade` moves the lock and leaves pyproject behind."""
    mod = _load()
    monkeypatch.delenv("ALLOW_DEP_DRIFT", raising=False)
    locked = _clean_interpreter(mod, monkeypatch)
    monkeypatch.setattr(mod, "declared_floors", lambda path: {"rich": "13.0.0"})

    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "1 declared floor(s) in pyproject.toml do not match uv.lock" in err
    assert f"    - rich: pyproject declares >=13.0.0, uv.lock pins {locked['rich']}" in err
    assert "  a direct dependency's lower bound must equal the version uv.lock pins" in err
    assert "  update pyproject.toml, then:  make sync-env" in err
    # Nothing is wrong with the interpreter here, so don't point at it.
    assert "interpreter:" not in err


def test_direct_dependency_without_a_lower_bound_fails_the_gate(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """An unbounded requirement is the worst case, not an exempt one: it claims
    every version ever published works."""
    mod = _load()
    monkeypatch.delenv("ALLOW_DEP_DRIFT", raising=False)
    locked = _clean_interpreter(mod, monkeypatch)
    monkeypatch.setattr(mod, "declared_floors", lambda path: {"pillow": None})

    assert mod.main() == 1
    err = capsys.readouterr().err
    assert (
        f"    - pillow: pyproject declares no lower bound, uv.lock pins {locked['pillow']}"
        in err
    )


def test_this_repo_declares_a_floor_equal_to_every_pin(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The invariant this class exists to hold, asserted against the real files."""
    mod = _load()
    floors = mod.declared_floors(_REPO_ROOT / "pyproject.toml")
    locked = mod.locked_versions(_REPO_ROOT / "uv.lock")
    assert floors, "no direct dependencies parsed out of pyproject.toml"
    # Non-canonical keys would silently miss every lookup against uv.lock.
    assert all(name == mod.canon(name) for name in floors)
    assert {n: f for n, f in floors.items() if f != locked.get(n)} == {}

    _clean_interpreter(mod, monkeypatch)
    assert mod.main() == 0


def test_allow_dep_drift_does_not_suppress_a_floor_mismatch(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The escape hatch is for installed-vs-locked drift only. Even with it set —
    and with stale installs downgraded to a warning — a wrong floor is fatal."""
    mod = _load()
    monkeypatch.setenv("ALLOW_DEP_DRIFT", "1")
    monkeypatch.setattr(mod, "_installed", lambda name: "0.0.0-drifted")
    monkeypatch.setattr(mod, "declared_floors", lambda path: {"rich": "13.0.0"})

    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "warning: " in err
    assert "do not match uv.lock" in err
