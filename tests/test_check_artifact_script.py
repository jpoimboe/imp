import subprocess
from pathlib import Path

SCRIPT = Path("src/imp/assets/workflows/default/scripts/check-artifact.sh")


def test_validates_passed_path(tmp_path: Path) -> None:
    good = tmp_path / "spec.md"
    good.write_text("content")
    r = subprocess.run(["bash", str(SCRIPT), str(good)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

    missing = tmp_path / "nope.md"
    r2 = subprocess.run(["bash", str(SCRIPT), str(missing)], capture_output=True, text=True)
    assert r2.returncode != 0
