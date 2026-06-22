from __future__ import annotations

import shutil
from pathlib import Path

from imp.engine.loader import load_workflow


def _imp_workflow(tmp_path: Path) -> Path:
    asset = Path("src/imp/assets/workflows/imp").resolve()
    dst = tmp_path / "wf"
    shutil.copytree(asset, dst)
    return dst


def test_persona_absent_yields_empty_string(tmp_path: Path) -> None:
    wf = _imp_workflow(tmp_path)
    (wf / "persona.md").unlink(missing_ok=True)
    assert load_workflow(wf).persona_source == ""


def test_persona_present_is_exposed_verbatim(tmp_path: Path) -> None:
    wf = _imp_workflow(tmp_path)
    (wf / "persona.md").write_text("# Doing tasks\nbe excellent\n")
    assert load_workflow(wf).persona_source == "# Doing tasks\nbe excellent\n"
