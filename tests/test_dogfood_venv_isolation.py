"""build_isolated_venv must produce a venv with NO access to the host's
site-packages, so the host's editable `imp` cannot shadow the copy
(dogfood Fix #2, [unit]-provable)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests._live import build_isolated_venv


def _write_stub_package(root: Path) -> None:
    (root / "src" / "impstub").mkdir(parents=True)
    (root / "src" / "impstub" / "__init__.py").write_text("VALUE = 'copy'\n")
    (root / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'impstub'\n"
        "version = '0.0.0'\n\n"
        "[build-system]\n"
        "requires = ['setuptools']\n"
        "build-backend = 'setuptools.build_meta'\n\n"
        "[tool.setuptools.packages.find]\n"
        "where = ['src']\n"
    )


def test_isolated_venv_has_no_host_site_packages(tmp_path: Path) -> None:
    pkg = tmp_path / "copy"
    pkg.mkdir()
    _write_stub_package(pkg)

    venv_python = build_isolated_venv(pkg)

    # The stub resolves to the copy we installed.
    probe = subprocess.run(
        [str(venv_python), "-c",
         "import impstub, sys; sys.stdout.write(impstub.__file__)"],
        capture_output=True, text=True, check=True,
    )
    resolved = Path(probe.stdout.strip()).resolve()
    assert str(resolved).startswith(str(pkg.resolve())), (
        f"impstub resolved to {resolved}, not under the copy {pkg}"
    )

    # A host-only package (pytest is present in the dev/CI env but is NOT a
    # dependency of the stub) must be UNREACHABLE — proving the venv has no
    # --system-site-packages, so nothing host-side can shadow the copy.
    blocked = subprocess.run(
        [str(venv_python), "-c", "import pytest"],
        capture_output=True, text=True,
    )
    assert blocked.returncode != 0, (
        "host site-packages leaked into the isolated venv; "
        "--system-site-packages must be OFF so the host editable imp "
        "cannot shadow the copy"
    )
