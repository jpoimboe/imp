"""A wheel built from the repo carries imp's docs corpus and resolves it in place."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SAMPLE = "guide/what-is-imp.md"
_PROBE = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from imp.assets import bundled_docs_dir; print(bundled_docs_dir())"
)


# No target flag makes `build` produce the sdist first, then the wheel from the extracted sdist.
@pytest.mark.parametrize("build_args", [["--wheel"], []], ids=["direct", "via-sdist"])
def test_the_wheel_ships_and_resolves_the_docs_corpus(
        build_args: list[str], tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    built = subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", *build_args,
         "--outdir", str(dist), str(_REPO_ROOT)],
        capture_output=True, text=True,
    )
    assert built.returncode == 0, built.stderr
    (wheel,) = dist.glob("*.whl")
    site = tmp_path / "site"
    with zipfile.ZipFile(wheel) as zf:
        assert f"imp/assets/docs/{_SAMPLE}" in zf.namelist()
        zf.extractall(site)

    probe = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(site)], capture_output=True, text=True)
    assert probe.returncode == 0, probe.stderr
    docs = Path(probe.stdout.strip())
    assert docs.is_relative_to(site / "imp" / "assets" / "docs")
    assert (docs / _SAMPLE).is_file()
