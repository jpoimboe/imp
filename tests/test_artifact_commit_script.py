"""artifact-commit.sh forwards <kind> to `imp artifact-commit`."""
from __future__ import annotations

import subprocess
from pathlib import Path

from imp.assets import bundled_workflow_dir

SCRIPT = bundled_workflow_dir("imp") / "scripts" / "artifact-commit.sh"


def _fake_imp(bin_dir: Path, marker: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    imp = bin_dir / "imp"
    imp.write_text(f'#!/usr/bin/env bash\necho "$@" > "{marker}"\n')
    imp.chmod(0o755)


def test_script_forwards_kind_to_imp(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    marker = tmp_path / "called.txt"
    _fake_imp(bin_dir, marker)
    proc = subprocess.run(
        ["bash", str(SCRIPT), "plan"],
        env={"IMP_PROJECT_ROOT": str(tmp_path),
             "PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert marker.read_text().strip() == "artifact-commit plan"


def test_script_requires_a_kind(tmp_path: Path) -> None:
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        env={"IMP_PROJECT_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True)
    assert proc.returncode != 0
