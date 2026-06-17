# tests/test_change_context.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import ChangeContext


def test_change_context_carries_workflow_dir(tmp_path: Path) -> None:
    wf = tmp_path / ".imp" / "change" / "workflow"
    cc = ChangeContext(
        handle="s", title="t", project_root=tmp_path, workflow_dir=wf,
    )
    assert cc.workflow_dir == wf


def test_change_context_workflow_dir_is_required(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        ChangeContext(handle="s", title="t", project_root=tmp_path)  # type: ignore[call-arg]
