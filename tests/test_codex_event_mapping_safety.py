from dataclasses import dataclass

from imp.engine.state import InMemoryEventLog
from imp.providers.codex_runner import process_event
from tests.codex_fakes import (
    FakeItemEvent,
    command_execution,
    file_change,
    web_search,
)


def test_web_search_emits_runner_warning() -> None:
    elog = InMemoryEventLog()
    state: dict = {}
    process_event(web_search("hot tip"), elog=elog, agent_id="step:a",
                  state=state, provider="codex", model="gpt-5.5")
    warns = [e for e in elog.read() if e["type"] == "runner_warning"]
    assert any("web_search" in str(w["payload"]) for w in warns)


def test_unknown_item_type_logs_fallback_does_not_raise() -> None:
    @dataclass
    class _Mystery:
        type: str = "todo_list"
    elog = InMemoryEventLog()
    state: dict = {}
    # MUST NOT raise on unknown item types (L3 default arm).
    process_event(FakeItemEvent(type="item.completed", item=_Mystery()),
                  elog=elog, agent_id="step:a", state=state,
                  provider="codex", model="gpt-5.5")
    warns = [e for e in elog.read() if e["type"] == "runner_warning"]
    assert any("todo_list" in str(w["payload"]) for w in warns)


def test_file_change_marks_native_violation_on_state() -> None:
    elog = InMemoryEventLog()
    state: dict = {"native_violation": None}
    process_event(file_change("/etc/passwd"), elog=elog, agent_id="step:a",
                  state=state, provider="codex", model="gpt-5.5")
    assert state["native_violation"] is not None
    # Path is captured for the runner_warning message.
    assert "/etc/passwd" in str(state["native_violation"])


def test_command_execution_marks_native_violation_on_state() -> None:
    elog = InMemoryEventLog()
    state: dict = {"native_violation": None}
    process_event(command_execution("rm -rf /"), elog=elog, agent_id="step:a",
                  state=state, provider="codex", model="gpt-5.5")
    assert state["native_violation"] is not None
    assert "rm -rf /" in str(state["native_violation"])
