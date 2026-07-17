from __future__ import annotations

from imp.assets import bundled_workflow_dir
from imp.engine.loader import DENYABLE_TOOLS, TOP_LEVEL_CAPS, load_workflow

REVIEW_AGENTS = (
    "spec-reviewer", "plan-reviewer", "gap-check",
    "integration-review", "batch-code-reviewer",
)


def test_save_and_exit_is_a_denyable_top_level_cap() -> None:
    assert "save_and_exit" in TOP_LEVEL_CAPS
    assert "save_and_exit" in DENYABLE_TOOLS


def test_conversation_agent_is_granted_save_and_exit() -> None:
    wf = load_workflow(bundled_workflow_dir("imp"))
    assert "save_and_exit" in wf.agents["threshold"].tools


def test_reviewers_are_denied_save_and_exit() -> None:
    wf = load_workflow(bundled_workflow_dir("imp"))
    for name in REVIEW_AGENTS:
        assert "save_and_exit" not in wf.agents[name].tools, name
