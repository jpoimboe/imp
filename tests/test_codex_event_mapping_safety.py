from dataclasses import dataclass
from typing import Any

from imp.engine.state import InMemoryEventLog
from imp.providers.codex_runner import process_event
from tests.codex_fakes import (
    FakeItemEvent,
    command_execution,
    file_change,
    web_search,
)


def test_web_search_emits_runner_warning_when_disabled() -> None:
    # Disabled state (agent did not declare web_search): tripwire warning.
    elog = InMemoryEventLog()
    state: dict[str, Any] = {}
    process_event(web_search("hot tip"), elog=elog, agent_id="step:a",
                  state=state, provider="codex", model="gpt-5.5")
    warns = [e for e in elog.read() if e["type"] == "runner_warning"]
    assert any("web_search" in str(w["payload"]) for w in warns)


def test_web_search_emits_tool_call_when_enabled() -> None:
    # Option B: when the agent declared web_search, a web_search item is the
    # expected model-hosted tool — surfaced as a tool_call (query only; codex's
    # WebSearchItem carries no results), NOT a tripwire warning.
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"web_search_enabled": True}
    process_event(web_search("hot tip"), elog=elog, agent_id="step:a",
                  state=state, provider="codex", model="gpt-5.5")
    events = elog.read()
    calls = [e for e in events if e["type"] == "tool_call"
             and e["payload"].get("name") == "web_search"]
    assert len(calls) == 1, events
    assert calls[0]["payload"]["input"] == {"query": "hot tip"}
    # No tripwire warning on the enabled path.
    warns = [e for e in events if e["type"] == "runner_warning"]
    assert not any("web_search" in str(w["payload"]) for w in warns), warns


def test_unknown_item_type_logs_fallback_does_not_raise() -> None:
    @dataclass
    class _Mystery:
        type: str = "todo_list"
    elog = InMemoryEventLog()
    state: dict[str, Any] = {}
    # MUST NOT raise on unknown item types (L3 default arm).
    process_event(FakeItemEvent(type="item.completed", item=_Mystery()),
                  elog=elog, agent_id="step:a", state=state,
                  provider="codex", model="gpt-5.5")
    warns = [e for e in elog.read() if e["type"] == "runner_warning"]
    assert any("todo_list" in str(w["payload"]) for w in warns)


def test_file_change_marks_native_violation_on_state() -> None:
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"native_violation": None}
    process_event(file_change("/etc/passwd"), elog=elog, agent_id="step:a",
                  state=state, provider="codex", model="gpt-5.5")
    assert state["native_violation"] is not None
    # Path is captured for the runner_warning message.
    assert "/etc/passwd" in str(state["native_violation"])


def test_command_execution_marks_native_violation_on_state() -> None:
    elog = InMemoryEventLog()
    state: dict[str, Any] = {"native_violation": None}
    process_event(command_execution("rm -rf /"), elog=elog, agent_id="step:a",
                  state=state, provider="codex", model="gpt-5.5")
    assert state["native_violation"] is not None
    assert "rm -rf /" in str(state["native_violation"])
