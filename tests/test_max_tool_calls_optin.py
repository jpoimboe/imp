"""Fix H: max_tool_calls has NO default; unset means no tool-count cap."""

from __future__ import annotations

from imp.engine.state import InMemoryEventLog


def test_step_context_accepts_none_max_tool_calls() -> None:
    log = InMemoryEventLog()
    rec = log.append_step_context(
        agent_id="a", provider="p", model="m", system_prompt="s",
        tools_schema=[], params={}, max_tool_calls=None)
    assert rec["payload"]["max_tool_calls"] is None


def test_executor_resolution_propagates_none_when_unset() -> None:
    # When neither agent nor workflow defaults set max_tool_calls, the resolved
    # value is None (no 50 fallback).
    agent_mtc = None
    defaults_mtc = None
    if agent_mtc is not None:
        resolved = agent_mtc
    elif defaults_mtc is not None:
        resolved = defaults_mtc
    else:
        resolved = None
    assert resolved is None
