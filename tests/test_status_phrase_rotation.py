from __future__ import annotations

import itertools

from imp.cli import narrative as nv
from tests._event_fixtures import ev

_PHRASE_FN = "imp.agents.front_door_status.front_door_status_phrase"


def _deterministic_draws(monkeypatch) -> None:
    counter = itertools.count(1)
    monkeypatch.setattr(_PHRASE_FN, lambda: f"rot{next(counter)}")


def _tool(eid: str, call_id: str) -> dict:
    return ev(eid, "tool_call", agent_id="a", name="read_file", id=call_id,
              input={"path": "x"})


def _prose(eid: str) -> dict:
    return ev(eid, "llm_response", agent_id="a",
              raw={"content": [{"type": "text", "text": "narration"}]}, usage={})


def _front_door_builder() -> nv.NarrativeBuilder:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "change_created", change_id=1, title="x"))
    b.add(ev("2", "step_started", step="front-door", kind="agent",
             phase="front-door", model="m", provider="anthropic",
             label="frozen0", rotate_phrase=True))
    return b


def test_phrase_rotates_after_each_tool_work_segment(monkeypatch) -> None:
    _deterministic_draws(monkeypatch)
    b = _front_door_builder()
    b.add(_tool("3", "c1"))
    seg1 = b.current_activity().verb
    b.add(_prose("4"))
    seg2 = b.current_activity().verb
    b.add(_tool("5", "c2"))
    b.add(_prose("6"))
    seg3 = b.current_activity().verb
    assert seg1 == "frozen0"          # initial frozen phrase shows through segment 1
    assert seg2 == "rot1"             # fresh draw at the first segment boundary
    assert seg3 == "rot2"             # a different fresh draw at the second
    assert seg2 != seg3


def test_pure_thinking_segment_advances_the_phrase(monkeypatch) -> None:
    _deterministic_draws(monkeypatch)
    b = _front_door_builder()
    b.add(ev("3", "thinking", agent_id="a", content="hmm"))
    b.add(_prose("4"))
    assert b.current_activity().verb == "rot1"


def test_no_rotation_when_a_segment_has_no_work(monkeypatch) -> None:
    monkeypatch.setattr(_PHRASE_FN, lambda: "SHOULD_NOT_DRAW")
    b = _front_door_builder()
    b.add(_prose("3"))                # prose boundary, no tool_call/thinking before it
    assert b.current_activity().verb == "frozen0"


def test_pushed_live_label_beats_the_rotated_phrase(monkeypatch) -> None:
    _deterministic_draws(monkeypatch)
    b = _front_door_builder()
    b.add(_tool("3", "c1"))
    b.add(_prose("4"))                # rotated_phrase == "rot1"
    b.add(ev("5", "step_label_updated", agent_id="a", label="pushed"))
    assert b.current_activity().verb == "pushed"


def test_rotation_leaves_the_frozen_journal_label_untouched(monkeypatch) -> None:
    _deterministic_draws(monkeypatch)
    b = _front_door_builder()
    b.add(_tool("3", "c1"))
    b.add(_prose("4"))
    step = b._step_stack[-1]
    assert step.rotated_phrase == "rot1"   # live phrase advanced
    assert step.label == "frozen0"         # frozen label read by static imp log


def test_step_without_rotate_phrase_keeps_its_label(monkeypatch) -> None:
    monkeypatch.setattr(_PHRASE_FN, lambda: "SHOULD_NOT_DRAW")
    b = nv.NarrativeBuilder()
    b.add(ev("1", "change_created", change_id=1, title="x"))
    b.add(ev("2", "step_started", step="task-lead", kind="agent", phase="code",
             model="m", provider="anthropic", label="task 1/6"))
    b.add(_tool("3", "c1"))
    b.add(_prose("4"))
    assert b.current_activity().verb == "task 1/6"


def test_phrase_rotates_at_a_user_message_boundary(monkeypatch) -> None:
    _deterministic_draws(monkeypatch)
    b = _front_door_builder()
    b.add(_tool("3", "c1"))
    b.add(ev("4", "user_message", agent_id="a", role="user",
             content=[{"type": "text", "text": "go"}]))
    assert b.current_activity().verb == "rot1"


def test_resume_reentry_resets_rotate_phrase_and_rotated_phrase(monkeypatch) -> None:
    _deterministic_draws(monkeypatch)
    b = _front_door_builder()
    b.add(_tool("3", "c1"))
    b.add(_prose("4"))
    step = b._step_stack[-1]
    assert step.rotated_phrase == "rot1"    # phrase advanced before resume
    b.add(ev("5", "workflow_resumed"))
    b.add(ev("6", "step_started", step="front-door", kind="agent",
             phase="front-door", model="m2", provider="anthropic",
             label="frozen1", rotate_phrase=True, restarted=True))
    assert b._step_stack[-1] is step        # same node reused
    assert step.rotated_phrase is None      # cleared on resume re-entry
    assert step.rotate_phrase is True       # re-armed from the new payload
