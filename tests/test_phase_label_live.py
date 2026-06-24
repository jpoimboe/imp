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
           phase_label="implementation phase · plan 1/3"),
    ]:
        b.add(e)
    return b


def test_live_line_prepends_phase_label_no_auto_thinking() -> None:
    # A pure-thinking agent turn: no live_label pushed, no tool burst -> the live
    # line shows phase · step · elapsed, with NO trailing `thinking` word
    # (criterion 8).
    line = ld.compose_activity_status(_build("finalizing"), frame=0, elapsed=3.0,
                                      color=False)
    assert "implementation phase · plan 1/3 · finalizing" in line
    assert "thinking" not in line


def test_live_line_does_not_double_pushed_thinking() -> None:
    b = _build("task 1/5")
    b.add(ev("3", "step_label_updated", agent_id="step:code/finalize",
             label="task 1/5 · thinking"))
    line = ld.compose_activity_status(b, frame=0, elapsed=2.0, color=False)
    assert "task 1/5 · thinking" in line
    assert "thinking · thinking" not in line
