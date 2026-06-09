"""Layer 1 (review-architecture spec): task-lead dispatches a sonnet implementer
plus a single sonnet quality-reviewer on BOTH paths; spec-task-reviewer is gone."""
from __future__ import annotations

from pathlib import Path

from imp.assets import default_workflow_dir
from imp.engine.loader import load_workflow

AGENTS = Path("src/imp/assets/workflows/default/agents")


def _task_lead():
    return load_workflow(default_workflow_dir()).agents["task-lead"]


def test_task_lead_subagents_drop_spec_task_reviewer() -> None:
    subs = set(_task_lead().subagents)
    assert subs == {"implementer", "quality-reviewer"}, subs
    assert "spec-task-reviewer" not in subs


def test_spec_task_reviewer_file_removed() -> None:
    assert not (AGENTS / "spec-task-reviewer.md").exists()


def test_task_lead_dispatches_implementer_at_medium_tier() -> None:
    body = _task_lead().prompt_template_source
    # The implementer moved haiku(low)->sonnet(medium); the old low-tier dispatch is gone.
    assert 'subagent_name="implementer", tier="medium"' in body
    assert 'subagent_name="implementer", tier="low"' not in body


def test_task_lead_dispatches_quality_reviewer_both_paths() -> None:
    body = _task_lead().prompt_template_source
    # quality-reviewer is the single per-task reviewer now; spec-task-reviewer is gone.
    assert 'subagent_name="quality-reviewer", tier="medium"' in body
    assert "spec-task-reviewer" not in body
