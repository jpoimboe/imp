# tests/test_quest_context.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import QuestContext


def test_quest_context_carries_workflow_dir(tmp_path: Path) -> None:
    wf = tmp_path / ".imp" / "quests" / "test-quest" / "workflow"
    cc = QuestContext(title="t", project_root=tmp_path, workflow_dir=wf)
    assert cc.workflow_dir == wf
    assert not hasattr(cc, "handle")


def test_quest_context_rejects_handle_argument(tmp_path: Path) -> None:
    wf = tmp_path / ".imp" / "quests" / "test-quest" / "workflow"
    with pytest.raises(TypeError):
        QuestContext(  # type: ignore[call-arg]
            handle="s", title="t", project_root=tmp_path, workflow_dir=wf
        )


def test_quest_context_workflow_dir_is_required(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        QuestContext(title="t", project_root=tmp_path)  # type: ignore[call-arg]
