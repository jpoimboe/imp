from __future__ import annotations

from imp.cli import live_driver as ld
from imp.cli import narrative as nv
from tests._event_fixtures import ev


def _build(label: str) -> nv.NarrativeBuilder:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="finalize", kind="agent", phase="code",
           model="m", provider="anthropic", label=label,
           phase_label="implementation phase · plan 1/3", position_label=True),
    ]:
        b.add(e)
    return b


def test_live_line_prepends_phase_label_no_auto_thinking() -> None:
    # position_label step: the breadcrumb rides the row's phase/plan/counter
    # segments; the verb ("thinking") belongs to the activity line, not the row.
    b = _build("finalizing")
    activity = b.current_activity()
    assert activity is not None
    row = ld.compose_position_row(
        activity=activity, quest_id=b.tree.root.id, title=b.tree.root.title,
        prompt_tokens=None, color=False, width=80)
    assert "implementation phase · plan 1/3 · finalizing" in row
    assert "thinking" not in row


def test_live_line_does_not_double_pushed_thinking() -> None:
    b = _build("task 1/5")
    b.add(ev("3", "step_label_updated", agent_id="step:code/finalize",
             label="task 1/5 · thinking"))
    activity = b.current_activity()
    assert activity is not None
    row = ld.compose_position_row(
        activity=activity, quest_id=b.tree.root.id, title=b.tree.root.title,
        prompt_tokens=None, color=False, width=80)
    assert "task 1/5 · thinking" in row
    assert "thinking · thinking" not in row  # pushed label is not doubled
