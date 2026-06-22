from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from imp.assets import bundled_workflow_dir
from imp.engine.change import Change
from imp.engine.executor import _render_label
from imp.engine.loader import load_workflow
from tests.conftest import _seed_control


def _change(tmp_path: Path) -> Change:
    _seed_control(tmp_path)
    return Change.create(tmp_path, title="t", change_id="aaa111", base_branch="main")


def _state(**vars: object) -> SimpleNamespace:
    return SimpleNamespace(vars=dict(vars))


def _label(name: str) -> str:
    wf = load_workflow(bundled_workflow_dir("imp"))
    return {s.name: s for s in wf.flat_steps()}[name].label


def test_position_vars_declared_with_defaults() -> None:
    d = load_workflow(bundled_workflow_dir("imp")).vars.defaults()
    assert d["plan_index"] == 1
    assert d["plan_total"] == 0
    assert d["task_total"] == 0


def test_plan_writer_label_renders_position(tmp_path: Path) -> None:
    ch = _change(tmp_path)
    label = _label("plan-writer")
    full = _render_label(label, ch, ch.event_log(), _state(plan_index=2, plan_total=4))
    assert full == "writing plan 2/4"
    bare = _render_label(label, ch, ch.event_log(), _state(plan_index=1, plan_total=0))
    assert bare == "writing plan 1"


def test_task_lead_label_renders_plan_position(tmp_path: Path) -> None:
    ch = _change(tmp_path)
    out = _render_label(_label("task-lead"), ch, ch.event_log(),
                        _state(plan_index=2, plan_total=4))
    assert out == "implementing · plan 2/4"
    bare = _render_label(_label("task-lead"), ch, ch.event_log(),
                         _state(plan_index=1, plan_total=0))
    assert bare == "implementing · plan 1"


def test_plan_writer_frontmatter_label_removed() -> None:
    # The workflow step label is authoritative; the agent frontmatter label is gone.
    assert load_workflow(bundled_workflow_dir("imp")).agents["plan-writer"].label is None
