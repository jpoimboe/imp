from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from imp.agents.prompts import render_label
from imp.assets import bundled_workflow_dir
from imp.engine.quest import Quest
from imp.engine.loader import load_workflow
from tests.conftest import _seed_control


def _change(tmp_path: Path) -> Quest:
    _seed_control(tmp_path)
    return Quest.create(tmp_path, title="t", quest_id="aaa111", base_branch="main")


def _state(**vars: object) -> SimpleNamespace:
    return SimpleNamespace(vars=dict(vars))


def _label(name: str) -> str:
    wf = load_workflow(bundled_workflow_dir("imp"))
    return {s.name: s for s in wf.flat_steps()}[name].label


def test_position_vars_declared_with_defaults() -> None:
    d = load_workflow(bundled_workflow_dir("imp")).vars.defaults()
    assert d["plan_index"] == 1
    assert d["plan_total"] == 1
    assert d["task_total"] == 0


def test_plan_writer_label_is_counter_free(tmp_path: Path) -> None:
    ch = _change(tmp_path)
    label = _label("plan-writer")
    assert render_label(label, ch, ch.event_log(),
                        _state(plan_index=2, plan_total=4)) == "writing plan"


def test_plan_reviewer_label_is_counter_free(tmp_path: Path) -> None:
    ch = _change(tmp_path)
    label = _label("plan-reviewer")
    assert render_label(label, ch, ch.event_log(),
                        _state(plan_index=2, plan_total=4)) == "reviewing plan"


def test_plan_finalized_label_is_counter_free(tmp_path: Path) -> None:
    ch = _change(tmp_path)
    label = _label("plan-finalized")
    assert render_label(label, ch, ch.event_log(),
                        _state(plan_index=2, plan_total=4)) == "finalizing plan"


def test_planning_phase_label_renders_plan_counter(tmp_path: Path) -> None:
    ch = _change(tmp_path)
    wf = load_workflow(bundled_workflow_dir("imp"))
    raw = {p.name: p for p in wf.phases}["plan"].label
    out = render_label(raw, ch, ch.event_log(), _state(plan_index=2, plan_total=4))
    assert out == "planning phase · plan 2/4"


def test_task_lead_label_is_position_only(tmp_path: Path) -> None:
    ch = _change(tmp_path)
    out = render_label(_label("task-lead"), ch, ch.event_log(), _state(task_total=6))
    assert out == "task 1/6"
    single = render_label(_label("task-lead"), ch, ch.event_log(), _state(task_total=1))
    assert single == "task 1/1"


def test_plan_writer_frontmatter_label_removed() -> None:
    # The workflow step label is authoritative; the agent frontmatter label is gone.
    assert load_workflow(bundled_workflow_dir("imp")).agents["plan-writer"].label is None
