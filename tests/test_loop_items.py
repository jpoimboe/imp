# tests/test_loop_items.py
import dataclasses

import pytest

from imp.agents.loop_items import (
    AwaitingToolResults,
    EndOfTurn,
    Error,
    Thinking,
    ToolRequested,
    Warning,
)


def test_tool_requested_carries_name_and_input():
    it = ToolRequested(id="t1", name="read_file", input={"path": "a.py"})
    assert it.id == "t1"
    assert it.name == "read_file"
    assert it.input == {"path": "a.py"}


def test_end_of_turn_defaults_stop_reason_none():
    it = EndOfTurn(last_text="done")
    assert it.last_text == "done"
    assert it.stop_reason is None


def test_awaiting_tool_results_carries_assistant_text():
    assert AwaitingToolResults(assistant_text="reading...").assistant_text == "reading..."
    assert AwaitingToolResults().assistant_text == ""


def test_error_wraps_full_payload():
    payload = {"category": "config", "kind": "tool_call_ceiling_exceeded", "ceiling": 3}
    assert Error(payload=payload).payload == payload


def test_thinking_and_warning_carry_text():
    assert Thinking(text="reasoning").text == "reasoning"
    assert Warning(text="heads up").text == "heads up"


def test_items_are_frozen():
    cases = [
        (Thinking(text="x"), "text"),
        (Warning(text="x"), "text"),
        (ToolRequested(id="t1", name="n", input={}), "name"),
        (AwaitingToolResults(), "assistant_text"),
        (EndOfTurn(last_text="x"), "last_text"),
        (Error(payload={}), "payload"),
    ]
    for instance, field in cases:
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(instance, field, None)  # type: ignore[misc]
