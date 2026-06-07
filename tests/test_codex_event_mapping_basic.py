from __future__ import annotations

from imp.engine.state import InMemoryEventLog
from imp.providers.codex_runner import process_event
from tests.codex_fakes import (
    agent_message,
    reasoning,
    thread_started,
    turn_completed,
)


def test_thread_started_records_thread_id() -> None:
    elog = InMemoryEventLog()
    state = {"thread_id": None}
    process_event(thread_started("thr-42"), elog=elog, agent_id="step:a",
                  state=state, provider="codex", model="gpt-5.5")
    assert state["thread_id"] == "thr-42"


def test_reasoning_appends_thinking_event() -> None:
    elog = InMemoryEventLog()
    process_event(reasoning("let me think"), elog=elog, agent_id="step:a",
                  state={}, provider="codex", model="gpt-5.5")
    events = [e for e in elog.read() if e["type"] == "thinking"]
    assert len(events) == 1
    assert events[0]["payload"]["content"] == "let me think"


def test_agent_message_appends_llm_response() -> None:
    # F1: the agent_message is buffered; the full-turn llm_response is emitted
    # once at turn.completed (carrying text + any tool_use blocks).
    elog = InMemoryEventLog()
    state = {"pending_usage": None, "auth_mode": "chatgpt"}
    process_event(agent_message("here is my plan"), elog=elog,
                  agent_id="step:a", state=state,
                  provider="codex", model="gpt-5.5")
    process_event(turn_completed(input_tokens=1, output_tokens=1), elog=elog,
                  agent_id="step:a", state=state,
                  provider="codex", model="gpt-5.5")
    events = [e for e in elog.read() if e["type"] == "llm_response"]
    assert len(events) == 1
    p = events[0]["payload"]
    assert p["provider"] == "codex"
    assert p["model"] == "gpt-5.5"
    assert "here is my plan" in p["raw"]["content"][0]["text"]
    # auth_mode is stamped through (Task 4 plumbed read_telemetry to pick it up).
    assert p["auth_mode"] == "chatgpt"


def test_agent_message_omits_auth_mode_when_none() -> None:
    elog = InMemoryEventLog()
    state = {"auth_mode": None}
    process_event(agent_message("hello"), elog=elog,
                  agent_id="step:a", state=state,
                  provider="codex", model="gpt-5.5")
    process_event(turn_completed(input_tokens=1, output_tokens=1), elog=elog,
                  agent_id="step:a", state=state,
                  provider="codex", model="gpt-5.5")
    events = [e for e in elog.read() if e["type"] == "llm_response"]
    assert len(events) == 1
    assert "auth_mode" not in events[0]["payload"]
