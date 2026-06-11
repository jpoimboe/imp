from __future__ import annotations

import imp_telegram
import pytest


@pytest.mark.parametrize(
    "event_type",
    ["awaiting_user_input", "errored", "merge_blocked", "completed"],
)
def test_loud_event_types_notify(event_type: str) -> None:
    assert imp_telegram.curate(event_type) == "loud"


@pytest.mark.parametrize("event_type", ["agent_output", "llm_response"])
def test_silent_event_types_are_quiet_narrative(event_type: str) -> None:
    assert imp_telegram.curate(event_type) == "silent"


@pytest.mark.parametrize(
    "event_type",
    ["tool_call", "tool_result", "thinking", "step_context", "workflow_paused"],
)
def test_dropped_event_types_are_dropped(event_type: str) -> None:
    assert imp_telegram.curate(event_type) is None


def test_unknown_event_type_is_dropped() -> None:
    assert imp_telegram.curate("something_new") is None
    assert imp_telegram.curate(None) is None
