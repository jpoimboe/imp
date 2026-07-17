"""engine_message/completion arms the WAITING-drop; the body seeds as agent_prompt."""

from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.narrative import NarrativeBuilder
from imp.cli.render_text import render_tree


def test_completion_nudge_handler_registered() -> None:
    assert "engine_message" in NarrativeBuilder._HANDLERS


def test_completion_nudge_renders_nothing() -> None:
    tree = nv.build_tree([
        {"id": "e1", "parent_id": None, "agent_id": None,
         "type": "engine_message",
         "payload": {"role": "user", "subtype": "completion",
                     "content": [{"type": "text",
                                  "text": "Your turn ended without complete_step..."}]}},
    ])
    out = "\n".join(render_tree(tree, full=False, color=False, width=80))
    assert "nudged" not in out
    assert "bare turn-end" not in out
    assert "warning:" not in out
    assert out == ""


def _nudge() -> dict:
    return {"id": "n1", "parent_id": None, "agent_id": "step:x",
            "type": "engine_message",
            "payload": {"role": "user", "subtype": "completion",
                        "content": [{"type": "text", "text": "bare turn-end"}]}}


def _text_response(eid: str, text: str) -> dict:
    return {"id": eid, "parent_id": None, "agent_id": "step:x",
            "type": "agent_message",
            "payload": {"provider": "anthropic",
                        "raw": {"content": [{"type": "text", "text": text}]}}}


def _tool_carrier(eid: str) -> dict:
    return {"id": eid, "parent_id": None, "agent_id": "step:x",
            "type": "agent_message",
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


def _render_full(events: list[dict]) -> str:
    tree = nv.build_tree(events)
    return "\n".join(render_tree(tree, full=True, color=False, width=80))


def _injected(eid: str, text: str) -> dict:
    return {"id": eid, "parent_id": None, "agent_id": "step:x",
            "type": "engine_message",
            "payload": {"role": "user", "subtype": "agent_prompt",
                        "content": [{"type": "text", "text": text}]}}


def test_nudge_user_message_suppressed_in_full() -> None:
    out = _render_full([
        _nudge(),
        _injected("u1", "Your turn ended without calling complete_step"),
    ])
    assert "injected message:" not in out
    assert "agent prompt:" in out
    assert "complete_step" in out


def test_non_nudge_injected_message_still_renders_in_full() -> None:
    out = _render_full([_injected("u1", "You are the spec-writer. Begin.")])
    assert "agent prompt:" in out
    assert "spec-writer" in out


def test_nudge_suppression_is_one_shot() -> None:
    out = _render_full([
        _nudge(),
        _injected("u1", "nudge injection body"),
        _injected("u2", "steer the agent here"),
    ])
    assert "nudge injection body" in out
    assert "steer the agent here" in out


def test_nudge_user_message_then_waiting_both_dropped() -> None:
    out = _render_full([
        _nudge(),
        _injected("u1", "nudge injection body"),
        _text_response("e1", "WAITING"),
    ])
    assert "nudge injection body" in out
    assert "WAITING" not in out
