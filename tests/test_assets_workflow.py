from __future__ import annotations

import shutil
from pathlib import Path

from imp.engine.loader import load_workflow


def test_shipped_default_workflow_loads(tmp_path: Path) -> None:
    asset = Path("src/imp/assets/workflows/default").resolve()
    shutil.copytree(asset, tmp_path / "wf")
    wf = load_workflow(tmp_path / "wf")
    assert wf.name
    assert wf.phases
    for agent in wf.agents.values():
        assert "complete_step" in agent.tools, f"{agent.name} missing complete_step"


def test_spec_writer_does_not_declare_ask_user(tmp_path: Path) -> None:
    asset = Path("src/imp/assets/workflows/default").resolve()
    shutil.copytree(asset, tmp_path / "wf")
    wf = load_workflow(tmp_path / "wf")
    spec_writer = wf.agents["spec-writer"]
    assert "ask_user" not in spec_writer.tools
    assert "complete_step" in spec_writer.tools
