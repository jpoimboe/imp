from __future__ import annotations

from imp.cli import narrative as nv
from tests._event_fixtures import ev


def _open() -> nv.NarrativeBuilder:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "quest_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="threshold", kind="agent",
             phase="open-front", phase_label="open-front phase",
             label="going live", model="claude"))
    return b


def test_dependency_waiting_sets_footer_verb_and_flag() -> None:
    b = _open()
    b.add(ev("3", "dependency_waiting", depends_on=["tuhe55", "koze82"]))
    act = b.current_activity()
    assert act.verb == "waiting for tuhe55, koze82 to merge"
    assert act.dep_wait is True


def test_single_dependency_verb() -> None:
    b = _open()
    b.add(ev("3", "dependency_waiting", depends_on=["tuhe55"]))
    assert b.current_activity().verb == "waiting for tuhe55 to merge"


def test_dependency_waiting_appends_no_scrollback_node() -> None:
    b = _open()
    b.add(ev("3", "dependency_waiting", depends_on=["tuhe55"]))
    step = b._step_stack[-1]
    assert [c for c in step.children
            if isinstance(c, nv.LiteralTextNode)
            and c.text.startswith("waiting for")] == []


def test_dependency_ready_reverts_footer() -> None:
    b = _open()
    b.add(ev("3", "dependency_waiting", depends_on=["tuhe55"]))
    b.add(ev("4", "dependency_ready"))
    assert b._dep_wait is None
    act = b.current_activity()
    assert act.dep_wait is False
    assert act.verb == "going live"


def test_start_ts_pinned_on_first_wait() -> None:
    b = _open()
    b.add(ev("3", "dependency_waiting", depends_on=["tuhe55"],
             timestamp="2026-06-03T00:00:03+00:00"))
    b.add(ev("4", "dependency_waiting", depends_on=["tuhe55"],
             timestamp="2026-06-03T00:00:09+00:00"))
    assert b._dep_wait.start_ts == "2026-06-03T00:00:03+00:00"


def test_step_completed_clears_dep_wait() -> None:
    b = _open()
    b.add(ev("3", "dependency_waiting", depends_on=["tuhe55"]))
    b.add(ev("4", "step_completed", step="threshold", kind="agent",
             action={"verb": "continue"}, var_mutations={}))
    assert b._dep_wait is None
