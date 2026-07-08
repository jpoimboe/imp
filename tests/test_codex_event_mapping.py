# tests/test_codex_event_mapping.py
from __future__ import annotations

import dataclasses
import json
import types as _types
from pathlib import Path
from typing import Any

from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.codex_mcp import RunState
from imp.providers.codex_runner import process_event
from tests.codex_fakes import (
    FakeAgentMessageItem,
    FakeItemEvent,
    FakeMcpToolCallItem,
    FakeTurnCompletedEvent,
    FakeUsage,
)


def _started(sdk_id: str, args: dict, *, tool: str = "sub_agent") -> Any:
    item = _types.SimpleNamespace(
        type="mcp_tool_call", tool=tool, name=tool, id=sdk_id,
        arguments=args, status="in_progress")
    return _types.SimpleNamespace(type="item.started", item=item)


def _completed(sdk_id: str, args: dict, *, result: str = "{}", tool: str = "sub_agent",
               error: Any = None, status: str = "completed") -> Any:
    item = _types.SimpleNamespace(
        type="mcp_tool_call", tool=tool, name=tool, id=sdk_id,
        arguments=args, result=result, error=error, status=status)
    return _types.SimpleNamespace(type="item.completed", item=item)


def _pe_kwargs(elog: Any, run_state: Any, state: Any) -> dict:
    return dict(elog=elog, agent_id="step:p/s", state=state, provider="codex",
                model="m", run_state=run_state)


def test_task_item_started_opens_fold_keyed_on_sdk_id() -> None:
    elog = InMemoryEventLog()
    run_state = RunState(cap=100)
    args = {"subagent_name": "impl", "task_description": "x"}
    process_event(_started("item_1", args), **_pe_kwargs(elog, run_state, {}))
    calls = [e for e in elog.read() if e["type"] == "tool_call"]
    assert len(calls) == 1
    assert calls[0]["payload"]["id"] == "item_1"
    assert calls[0]["payload"]["name"] == "sub_agent"
    assert [e for e in elog.read() if e["type"] == "tool_result"] == []


def test_task_item_completed_closes_fold_same_id_id_ns_event() -> None:
    elog = InMemoryEventLog()
    run_state = RunState(cap=100)
    args = {"subagent_name": "impl", "task_description": "x"}
    st: dict = {}
    process_event(_started("item_1", args), **_pe_kwargs(elog, run_state, st))
    process_event(_completed("item_1", args, result='{"status":"Success"}'),
                  **_pe_kwargs(elog, run_state, st))
    calls = [e for e in elog.read() if e["type"] == "tool_call"]
    results = [e for e in elog.read() if e["type"] == "tool_result"]
    assert len(calls) == 1                              # completed does NOT re-open
    assert len(results) == 1
    assert results[0]["payload"]["id"] == "item_1"      # pairs to the started id
    assert results[0]["payload"]["id_ns"] == "event"
    assert results[0]["payload"]["error"] is False


def test_two_task_calls_pair_by_sdk_id_out_of_order() -> None:
    elog = InMemoryEventLog()
    run_state = RunState(cap=100)
    a = {"subagent_name": "A", "task_description": "x"}
    b = {"subagent_name": "B", "task_description": "y"}
    st: dict = {}
    kw = _pe_kwargs(elog, run_state, st)
    process_event(_started("item_1", a), **kw)
    process_event(_started("item_2", b), **kw)
    process_event(_completed("item_2", b, result='{"r":"B"}'), **kw)   # out of order
    process_event(_completed("item_1", a, result='{"r":"A"}'), **kw)
    results = {e["payload"]["id"]: e["payload"]["content"]
               for e in elog.read() if e["type"] == "tool_result"}
    assert results["item_2"] == '{"r":"B"}'
    assert results["item_1"] == '{"r":"A"}'


def test_task_completed_without_started_emits_lone_result() -> None:
    """AC5 no-pair path: a task item.completed with no prior item.started emits a
    lone keyed tool_result (no re-opened call, no crash)."""
    elog = InMemoryEventLog()
    run_state = RunState(cap=100)
    a = {"subagent_name": "A", "task_description": "x"}
    process_event(_completed("item_9", a, result="ceiling"),
                  **_pe_kwargs(elog, run_state, {}))
    rows = list(elog.read())
    assert [e for e in rows if e["type"] == "tool_call"] == []
    res = [e for e in rows if e["type"] == "tool_result"]
    assert len(res) == 1 and res[0]["payload"]["id"] == "item_9"
    assert res[0]["payload"]["id_ns"] == "event"


def test_non_task_item_completed_still_emits_call_and_result() -> None:
    """Non-task tools are unaffected: item.completed emits BOTH the call and result
    keyed on the SDK id (no item.started handling for them)."""
    elog = InMemoryEventLog()
    run_state = RunState(cap=100)
    process_event(_completed("bash-1", {"command": "echo hi"}, tool="bash",
                             result="hi"),
                  **_pe_kwargs(elog, run_state, {}))
    rows = list(elog.read())
    assert len([e for e in rows if e["type"] == "tool_call"]) == 1
    assert len([e for e in rows if e["type"] == "tool_result"]) == 1
    assert "id_ns" not in next(e for e in rows if e["type"] == "tool_result")["payload"]


def test_preamble_prose_llm_response_precedes_tool_calls() -> None:
    elog = InMemoryEventLog()
    state = {"web_search_enabled": False}
    run_state = RunState(cap=10, agent_id="step:t")
    msg = FakeItemEvent(type="item.completed",
                        item=FakeAgentMessageItem(text="editing now"))
    call = FakeItemEvent(type="item.completed", item=FakeMcpToolCallItem(
        tool="write_file", arguments={"path": "x"}, result="ok", id="c1"))
    done = FakeTurnCompletedEvent(usage=FakeUsage(input_tokens=10, output_tokens=5))
    for ev in (msg, call, done):
        process_event(ev, elog=elog, agent_id="step:t", state=state,
                      provider="codex", model="gpt-5.5", run_state=run_state)
    types = [e["type"] for e in elog.read()]
    assert types.index("llm_response") < types.index("tool_call")
    llms = [e for e in elog.read() if e["type"] == "llm_response"]
    assert len(llms) == 1
    content = llms[0]["payload"]["raw"]["content"]
    assert content == [{"type": "text", "text": "editing now"}]
    assert all(b["type"] != "tool_use" for b in content)
    assert llms[0]["payload"]["usage"]  # usage attached to the prose response


def test_summary_prose_llm_response_follows_tool_calls() -> None:
    elog = InMemoryEventLog()
    state = {"web_search_enabled": False}
    run_state = RunState(cap=10, agent_id="step:t")
    call = FakeItemEvent(type="item.completed", item=FakeMcpToolCallItem(
        tool="write_file", arguments={"path": "x"}, result="ok", id="c1"))
    msg = FakeItemEvent(type="item.completed",
                        item=FakeAgentMessageItem(text="all done"))
    done = FakeTurnCompletedEvent(usage=FakeUsage(input_tokens=10, output_tokens=5))
    for ev in (call, msg, done):
        process_event(ev, elog=elog, agent_id="step:t", state=state,
                      provider="codex", model="gpt-5.5", run_state=run_state)
    types = [e["type"] for e in elog.read()]
    assert types.index("tool_call") < types.index("llm_response")
    llms = [e for e in elog.read() if e["type"] == "llm_response"]
    assert len(llms) == 1
    assert llms[0]["payload"]["raw"]["content"] == [{"type": "text", "text": "all done"}]


def test_text_only_turn_flushes_prose_at_turn_completed() -> None:
    elog = InMemoryEventLog()
    state = {"web_search_enabled": False}
    run_state = RunState(cap=10, agent_id="step:t")
    msg = FakeItemEvent(type="item.completed",
                        item=FakeAgentMessageItem(text="just chatting"))
    done = FakeTurnCompletedEvent(usage=FakeUsage(input_tokens=10, output_tokens=5))
    for ev in (msg, done):
        process_event(ev, elog=elog, agent_id="step:t", state=state,
                      provider="codex", model="gpt-5.5", run_state=run_state)
    llms = [e for e in elog.read() if e["type"] == "llm_response"]
    assert len(llms) == 1
    assert llms[0]["payload"]["raw"]["content"] == [{"type": "text", "text": "just chatting"}]
    assert llms[0]["payload"]["usage"]
    assert [e for e in elog.read() if e["type"] == "tool_call"] == []


def test_prose_less_turn_emits_empty_prose_usage_carrier() -> None:
    elog = InMemoryEventLog()
    state = {"web_search_enabled": False}
    run_state = RunState(cap=10, agent_id="step:t")
    call = FakeItemEvent(type="item.completed", item=FakeMcpToolCallItem(
        tool="write_file", arguments={"path": "x"}, result="ok", id="c1"))
    done = FakeTurnCompletedEvent(usage=FakeUsage(input_tokens=10, output_tokens=5))
    for ev in (call, done):
        process_event(ev, elog=elog, agent_id="step:t", state=state,
                      provider="codex", model="gpt-5.5", run_state=run_state)
    llms = [e for e in elog.read() if e["type"] == "llm_response"]
    assert len(llms) == 1
    assert llms[0]["payload"]["raw"]["content"] == []      # empty-prose carrier
    assert llms[0]["payload"]["usage"]                      # usage attached to it


def test_preamble_before_subagent_dispatch_leads_tool_call() -> None:
    elog = InMemoryEventLog()
    run_state = RunState(cap=100)
    args = {"subagent_name": "impl", "task_description": "x"}
    st: dict = {"web_search_enabled": False}
    kw = _pe_kwargs(elog, run_state, st)
    msg = FakeItemEvent(type="item.completed",
                        item=FakeAgentMessageItem(text="dispatching impl"))
    process_event(msg, **kw)
    process_event(_started("item_1", args), **kw)   # task tool_call opens HERE
    process_event(_completed("item_1", args, result='{"status":"Success"}'), **kw)
    process_event(FakeTurnCompletedEvent(
        usage=FakeUsage(input_tokens=5, output_tokens=1)), **kw)
    types = [e["type"] for e in elog.read()]
    assert types.index("llm_response") < types.index("tool_call")
    llms = [e for e in elog.read() if e["type"] == "llm_response"]
    assert len(llms) == 1
    assert llms[0]["payload"]["raw"]["content"] == [{"type": "text", "text": "dispatching impl"}]


def test_non_task_mcp_result_is_unstamped() -> None:
    # AC38: a non-task tool result must NOT be stamped (id_ns is task-only).
    elog = InMemoryEventLog()
    process_event(
        FakeItemEvent(type="item.completed", item=FakeMcpToolCallItem(
            tool="edit", arguments={"path": "a.txt"}, result="ok", id="sdk-edit")),
        elog=elog, agent_id="step:t", state={"web_search_enabled": False},
        provider="codex", model="gpt-5.5", run_state=RunState(cap=10, agent_id="step:t"))
    result = next(e for e in elog.read() if e["type"] == "tool_result")
    assert "id_ns" not in result["payload"]


def _labeled_def(name: str, label: str) -> Any:
    base = AgentDef(
        name=name, description="", tools=frozenset(), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="hi", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1))
    return dataclasses.replace(base, label=label)


def test_task_item_started_stamps_subagent_label() -> None:
    elog = InMemoryEventLog()
    run_state = RunState(cap=100)
    args = {"subagent_name": "explorer", "task_description": "x"}
    process_event(
        _started("item_1", args),
        **_pe_kwargs(elog, run_state, {}),
        subagent_defs={"explorer": _labeled_def("explorer", "exploring codebase")})
    call = next(e for e in elog.read() if e["type"] == "tool_call")
    assert call["payload"]["name"] == "sub_agent"
    assert call["payload"]["label"] == "exploring codebase"


def test_task_item_started_omits_label_when_def_unknown() -> None:
    elog = InMemoryEventLog()
    run_state = RunState(cap=100)
    args = {"subagent_name": "ghost", "task_description": "x"}
    process_event(
        _started("item_1", args),
        **_pe_kwargs(elog, run_state, {}),
        subagent_defs={})
    call = next(e for e in elog.read() if e["type"] == "tool_call")
    assert "label" not in call["payload"]


def test_task_item_started_handles_string_arguments() -> None:
    elog = InMemoryEventLog()
    run_state = RunState(cap=100)
    args = json.dumps({"subagent_name": "explorer", "task_description": "x"})
    process_event(
        _started("item_1", args),
        **_pe_kwargs(elog, run_state, {}),
        subagent_defs={"explorer": _labeled_def("explorer", "exploring codebase")})
    call = next(e for e in elog.read() if e["type"] == "tool_call")
    assert call["payload"]["label"] == "exploring codebase"
