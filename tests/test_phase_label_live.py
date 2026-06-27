from __future__ import annotations

from imp.cli import live_driver as ld
from imp.cli import narrative as nv
from tests._event_fixtures import ev


def _build(label: str) -> nv.NarrativeBuilder:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="finalize", kind="agent", phase="code",
           model="m", provider="anthropic", label=label,
           phase_label="implementation phase · plan 1/3", position_label=True),
    ]:
        b.add(e)
    return b


def test_live_line_prepends_phase_label_no_auto_thinking() -> None:
    # position_label step: position = phase_label · label; verb = "thinking".
    # The full breadcrumb appears in the position bar (not the activity line).
    b = _build("finalizing")
    line = ld.compose_activity_status(b, frame=0, elapsed=1.0, color=False, width=80)
    assert line is not None
    assert "implementation phase · plan 1/3 · finalizing" in line
    assert line.count("thinking") == 1  # verb on the activity line only, not the position bar


def test_live_line_does_not_double_pushed_thinking() -> None:
    b = _build("task 1/5")
    b.add(ev("3", "step_label_updated", agent_id="step:code/finalize",
             label="task 1/5 · thinking"))
    line = ld.compose_activity_status(b, frame=0, elapsed=1.0, color=False, width=80)
    assert line is not None
    assert "task 1/5 · thinking" in line
    assert "thinking · thinking" not in line  # pushed label is not doubled
