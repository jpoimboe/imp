from __future__ import annotations

from imp.assets import bundled_workflow_dir
from imp.engine.loader import BASE_CAPS, load_workflow


def test_new_tools_are_in_base_caps() -> None:
    assert "update_step_label" in BASE_CAPS
    assert "emit_notice" in BASE_CAPS


def test_task_lead_receives_progress_tools() -> None:
    wf = load_workflow(bundled_workflow_dir("imp"))
    assert "update_step_label" in wf.agents["task-lead"].tools
    assert "emit_notice" in wf.agents["task-lead"].tools
