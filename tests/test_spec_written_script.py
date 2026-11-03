"""The spec-written gate: on the express path the threshold draft spec must be on
disk. Present -> the marker on stdout, exit 0; missing -> recovery on stderr, exit 1."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from imp.assets import bundled_workflow_dir

SCRIPT = bundled_workflow_dir("default") / "scripts" / "spec-written.sh"


def _run(root: Path, quest_dir: Path, quest_id: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=root,
        env={"IMP_PROJECT_ROOT": str(root), "IMP_QUEST_DIR": str(quest_dir),
             "IMP_QUEST_ID": quest_id, "PATH": os.environ["PATH"]},
        # encoding, not text=True: the output carries '✓', '·' and '—', and env= drops
        # the locale, so decoding must not depend on the host's default encoding.
        capture_output=True, encoding="utf-8",
    )


def test_spec_present_prints_marker(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    home = tmp_path / "home"
    spec = home / "artifacts" / "spec" / "ab12cd.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# Spec\n")
    proc = _run(root, home, "ab12cd")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "✓ spec written · imp show ab12cd spec\n"
    assert proc.stderr == ""


def test_spec_missing_resumes_with_recovery(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    home = tmp_path / "home"
    (home / "artifacts" / "spec").mkdir(parents=True)
    proc = _run(root, home, "ab12cd")
    want = home / "artifacts" / "spec" / "ab12cd.md"
    assert want.is_absolute()
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert proc.stderr == (
        f"No spec at {want} — write the draft spec there, then finish the step again.\n"
    )


def test_other_quests_spec_does_not_count(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    home = tmp_path / "home"
    other = home / "artifacts" / "spec" / "zz9999.md"
    other.parent.mkdir(parents=True)
    other.write_text("# Other\n")
    proc = _run(root, home, "ab12cd")
    assert proc.returncode == 1
    assert proc.stdout == ""
