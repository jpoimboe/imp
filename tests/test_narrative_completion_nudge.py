"""completion_nudge is telemetry-only: it renders nothing in the narrative."""

from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.narrative import NarrativeBuilder
from imp.cli.render_text import render_tree


def test_completion_nudge_handler_registered() -> None:
    assert "completion_nudge" in NarrativeBuilder._HANDLERS


def test_completion_nudge_renders_nothing() -> None:
    tree = nv.build_tree([
        {"id": "e1", "parent_id": None, "agent_id": None,
         "type": "completion_nudge",
         "payload": {"message": "Your turn ended without complete_step..."}},
    ])
    out = "\n".join(render_tree(tree, full=False, color=False, width=80))
    assert "nudged" not in out
    assert "bare turn-end" not in out
    assert "warning:" not in out
    assert out == ""


def _nudge() -> dict:
    return {"id": "n1", "parent_id": None, "agent_id": "step:x",
            "type": "completion_nudge",
            "payload": {"message": "bare turn-end"}}


def _text_response(eid: str, text: str) -> dict:
    return {"id": eid, "parent_id": None, "agent_id": "step:x",
            "type": "llm_response",
            "payload": {"provider": "anthropic",
                        "raw": {"content": [{"type": "text", "text": text}]}}}


def _tool_carrier(eid: str) -> dict:
    return {"id": eid, "parent_id": None, "agent_id": "step:x",
            "type": "llm_response",
            "payload": {"provider": "anthropic",
                        "raw": {"content": [
                            {"type": "tool_use", "id": "t1",
                             "name": "read_file", "input": {}}]}}}


def _render(events: list[dict]) -> str:
    tree = nv.build_tree(events)
    return "\n".join(render_tree(tree, full=False, color=False, width=80))


def test_nudge_then_waiting_reply_dropped() -> None:
    out = _render([_nudge(), _text_response("e1", "WAITING")])
    assert "WAITING" not in out


def test_nudge_then_waiting_reply_dropped_case_insensitive() -> None:
    out = _render([_nudge(), _text_response("e1", "waiting")])
    assert "waiting" not in out.lower()


def test_nudge_then_substantive_prose_renders() -> None:
    out = _render([_nudge(), _text_response("e1", "Here is my analysis of the merge.")])
    assert "analysis" in out


def test_waiting_reply_without_nudge_renders() -> None:
    out = _render([_text_response("e1", "WAITING")])
    assert "WAITING" in out


def test_nudge_flag_survives_intervening_nonprose() -> None:
    out = _render([_nudge(), _tool_carrier("e1"), _text_response("e2", "WAITING")])
    assert "WAITING" not in out


def _thinking(eid: str) -> dict:
    return {"id": eid, "parent_id": None, "agent_id": "step:x",
            "type": "thinking", "payload": {"content": "deliberating"}}


def test_nudge_flag_survives_intervening_thinking() -> None:
    out = _render([_nudge(), _thinking("t1"), _text_response("e2", "WAITING")])
    assert "WAITING" not in out


def test_nudge_then_waiting_reply_dropped_mixed_case() -> None:
    out = _render([_nudge(), _text_response("e1", "Waiting")])
    assert "waiting" not in out.lower()
