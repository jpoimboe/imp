from __future__ import annotations

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import InMemoryEventLog


def _render_task_lead(**var_overrides) -> str:
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["task-lead"].prompt_template_source
    vars = {"fast_path": False, "plan_index": 2, "plan_total": 4, "task_total": 6,
            **var_overrides}
    ctx = build_context(change_title="t", event_log=InMemoryEventLog(),
                        quest_id="chg", quest_home=".imp/changes/chg",
                        vars=vars)
    return render_prompt(body, ctx, workflow_dir=bundled_workflow_dir("imp"))


def test_task_lead_pushes_position_only_no_verbs() -> None:
    out = _render_task_lead()
    assert "task <N>/6" in out          # the one position push at task pick
    # Exactly one position push: the spec decision is "push once per task". A
    # regression that de-verbed a flip instead of deleting it would leave extra
    # pushes, so lock the count at one.
    assert out.count('update_step_label(label="task <N>/') == 1
    assert "· thinking" not in out
    assert "· coding" not in out
    assert "· reviewing" not in out


def test_task_lead_live_label_drops_the_phase_prefix() -> None:
    # The code-phase label now supplies `implementation phase · plan i/N`; the pushed
    # live label carries only the trailing `task …` segments.
    out = _render_task_lead()
    assert "code · plan" not in out
