from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.engine.state import InMemoryEventLog, JsonlEventLog


def _read(path: Path) -> list[dict]:
    return list(JsonlEventLog(path).read())


def test_typed_appends_have_envelope(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "e.jsonl")
    ev = log.append_step_context(
        agent_id="step:p/s", parent_id_override="seed-1",
        provider="anthropic", model="m", system_prompt="sys",
        tools_schema=[{"name": "read_file"}], params={"max_tokens": 8192},
        max_tool_calls=50,
    )
    assert set(ev) == {"id", "parent_id", "agent_id", "timestamp", "type", "payload"}
    assert ev["type"] == "step_context"
    assert ev["agent_id"] == "step:p/s"
    assert ev["parent_id"] == "seed-1"      # override honored on first append
    assert ev["payload"]["provider"] == "anthropic"
    assert ev["payload"]["max_tool_calls"] == 50


def test_auto_chain_within_agent(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "e.jsonl")
    a = log.append_step_context(
        agent_id="step:p/s", parent_id_override="seed",
        provider="anthropic", model="m", system_prompt="s",
        tools_schema=[], params={}, max_tool_calls=1,
    )
    b = log.append_llm_response(
        agent_id="step:p/s", provider="anthropic", model="m",
        raw={"content": []}, usage={"input_tokens": 1}, stop_reason="end_turn",
    )
    assert b["parent_id"] == a["id"]        # auto-chain, no override


def test_separate_agents_dont_cross_link(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "e.jsonl")
    log.append_llm_response(agent_id="A", provider="p", model="m",
                            raw={}, usage={}, stop_reason=None)
    b = log.append_llm_response(agent_id="B", provider="p", model="m",
                                raw={}, usage={}, stop_reason=None)
    assert b["parent_id"] is None           # first event for agent B


def test_executor_level_events_never_chain(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "e.jsonl")
    one = log.append(agent_id=None, type="step_started", payload={"step": "s"})
    two = log.append(agent_id=None, type="step_completed", payload={"step": "s"})
    assert one["parent_id"] is None
    assert two["parent_id"] is None         # agent_id=None does NOT chain
    assert one["id"]                        # id still populated


def test_chain_rebuild_on_construction(tmp_path: Path) -> None:
    p = tmp_path / "e.jsonl"
    first = JsonlEventLog(p)
    seed = first.append_step_context(
        agent_id="step:p/s", parent_id_override="root",
        provider="a", model="m", system_prompt="s",
        tools_schema=[], params={}, max_tool_calls=1)
    # Fresh instance over the same file rebuilds last[agent_id] by scanning.
    second = JsonlEventLog(p)
    nxt = second.append_llm_response(
        agent_id="step:p/s", provider="a", model="m",
        raw={}, usage={}, stop_reason=None)
    assert nxt["parent_id"] == seed["id"]


def test_on_event_fan_out_fires_once_with_persisted_dict(tmp_path: Path) -> None:
    seen: list[dict] = []
    log = JsonlEventLog(tmp_path / "e.jsonl", on_event=seen.append)
    ev = log.append(agent_id=None, type="phase_started", payload={"phase": "p"})
    assert seen == [ev]
    on_disk = _read(tmp_path / "e.jsonl")
    assert on_disk == [ev]


def test_append_user_message_serializes_text(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "e.jsonl")
    msg = Message(role="user", content=(TextBlock(text="hello"),))
    ev = log.append_user_message(agent_id="step:p/s", message=msg)
    assert ev["payload"] == {"role": "user",
                             "content": [{"type": "text", "text": "hello"}]}


def test_append_tool_call_and_result_shapes(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "e.jsonl")
    c = log.append_tool_call(
        agent_id="A", block=ToolUseBlock(id="t1", name="read_file", input={"p": 1}))
    assert c["payload"] == {"id": "t1", "name": "read_file", "input": {"p": 1}}
    r = log.append_tool_result(
        agent_id="A", block=ToolResultBlock(tool_use_id="t1", content="ok"))
    assert r["payload"] == {"id": "t1", "content": "ok", "error": False}


def test_strict_mode_raises_on_bad_payload(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "e.jsonl", strict=True)
    with pytest.raises(ValueError):
        log.append(agent_id=None, type="step_started", payload="not-a-dict")


def test_lenient_mode_coerces_and_warns(tmp_path: Path) -> None:
    seen: list[dict] = []
    log = JsonlEventLog(tmp_path / "e.jsonl", on_event=seen.append)  # strict defaults False
    log.append(agent_id="A", type="llm_response", payload="not-a-dict")
    types = [e["type"] for e in _read(tmp_path / "e.jsonl")]
    # best-effort write of the original event PLUS a runner_warning
    assert "llm_response" in types
    assert "runner_warning" in types


def test_inmemory_matches_jsonl_shape(tmp_path: Path) -> None:
    mem = InMemoryEventLog()
    jl = JsonlEventLog(tmp_path / "e.jsonl")
    for log in (mem, jl):
        log.append_step_context(
            agent_id="A", parent_id_override="seed",
            provider="a", model="m", system_prompt="s",
            tools_schema=[], params={}, max_tool_calls=1)
    mem_ev = next(iter(mem.read()))
    jl_ev = next(iter(jl.read()))
    assert set(mem_ev) == set(jl_ev)
    assert mem_ev["type"] == jl_ev["type"] == "step_context"


def test_append_tool_call_stamps_label_as_sibling(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "e.jsonl")
    c = log.append_tool_call(
        agent_id="A",
        block=ToolUseBlock(id="t1", name="sub_agent",
                           input={"subagent_name": "explorer"}),
        label="exploring codebase")
    assert c["payload"]["label"] == "exploring codebase"
    # input stays the pure model arguments — no label folded in
    assert c["payload"]["input"] == {"subagent_name": "explorer"}


def test_append_tool_call_omits_label_when_none(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "e.jsonl")
    c = log.append_tool_call(
        agent_id="A", block=ToolUseBlock(id="t1", name="read_file", input={"p": 1}))
    assert "label" not in c["payload"]


def test_append_tool_result_stamps_id_ns_when_provided() -> None:
    # AC38 (plumbing): the optional id_ns keyword stamps the tool_result payload;
    # omitting it leaves the payload shape unchanged (no "id_ns" key) so legacy
    # and non-task results stay byte-identical.
    from imp.agents.runner import ToolResultBlock
    from imp.engine.state import InMemoryEventLog

    elog = InMemoryEventLog()
    elog.append_tool_result(
        agent_id="step:p/s",
        block=ToolResultBlock("b1", '{"status":"Success"}', is_error=False),
        id_ns="block")
    elog.append_tool_result(
        agent_id="step:p/s",
        block=ToolResultBlock("b2", "ok", is_error=False))

    results = [e for e in elog.read() if e["type"] == "tool_result"]
    by_id = {e["payload"]["id"]: e["payload"] for e in results}
    assert by_id["b1"]["id_ns"] == "block"      # stamped
    assert "id_ns" not in by_id["b2"]           # omitted → absent, not None


def test_append_query_result_carries_telemetry_and_omits_absent(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "e.jsonl")
    r = log.append_query_result(
        agent_id="A", total_cost_usd=0.0123, duration_ms=4200,
        duration_api_ms=3900, num_turns=3, stop_reason="end_turn",
        subtype="success", is_error=False,
        usage={"input_tokens": 1000, "output_tokens": 200},
        model_usage={"claude-x": {"input_tokens": 1000}})
    assert r["type"] == "query_result"
    p = r["payload"]
    assert p["total_cost_usd"] == 0.0123
    assert p["num_turns"] == 3
    assert p["usage"] == {"input_tokens": 1000, "output_tokens": 200}
    assert p["model_usage"] == {"claude-x": {"input_tokens": 1000}}
    assert p["duration_ms"] == 4200
    assert p["duration_api_ms"] == 3900
    assert p["stop_reason"] == "end_turn"
    assert p["subtype"] == "success"
    assert p["is_error"] is False
    # absent optional SDK fields are omitted, never written as None
    assert "api_error_status" not in p
    assert "permission_denials" not in p
    assert "errors" not in p


def test_append_query_result_includes_present_optional_fields(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "e.jsonl")
    r = log.append_query_result(
        agent_id="A", total_cost_usd=0.0, duration_ms=1, duration_api_ms=1,
        num_turns=1, stop_reason=None, subtype="error_during_execution",
        is_error=True, usage={}, model_usage={},
        api_error_status=529, permission_denials=[{"tool": "bash"}],
        errors=["boom"])
    p = r["payload"]
    assert p["api_error_status"] == 529
    assert p["permission_denials"] == [{"tool": "bash"}]
    assert p["errors"] == ["boom"]


def test_append_llm_response_stamps_optional_fields_when_provided(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "e.jsonl")
    a = log.append_llm_response(
        agent_id="A", provider="claude_code", model="served-x",
        raw={"content": []}, usage={}, stop_reason="end_turn",
        message_id="msg_1", request_id="req_9", duration_ms=1234)
    assert a["payload"]["message_id"] == "msg_1"
    assert a["payload"]["request_id"] == "req_9"
    assert a["payload"]["duration_ms"] == 1234
    b = log.append_llm_response(
        agent_id="A", provider="anthropic", model="m",
        raw={"content": []}, usage={}, stop_reason="end_turn")
    assert "message_id" not in b["payload"]
    assert "request_id" not in b["payload"]
    assert "duration_ms" not in b["payload"]


def test_append_tool_result_stamps_duration_ms_when_provided(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "e.jsonl")
    a = log.append_tool_result(
        agent_id="A", block=ToolResultBlock(tool_use_id="t1", content="ok"),
        duration_ms=37.5)
    assert a["payload"]["duration_ms"] == 37.5
    b = log.append_tool_result(
        agent_id="A", block=ToolResultBlock(tool_use_id="t2", content="ok"))
    assert "duration_ms" not in b["payload"]


def test_in_memory_patch_lands_context_tokens_on_llm_response() -> None:
    log = InMemoryEventLog()
    log.append_llm_response(agent_id="step:a", provider="codex", model="m",
                            raw={}, usage={}, stop_reason=None)
    log.update_last_llm_response_usage(
        agent_id="step:a",
        usage={"input_tokens": 800, "cache_read_input_tokens": 200},
        context_tokens=1000)
    resp = next(e for e in log.read() if e["type"] == "llm_response")
    assert resp["payload"]["context_tokens"] == 1000
    assert resp["payload"]["usage"]["cache_read_input_tokens"] == 200


def test_in_memory_patch_omits_context_tokens_when_none() -> None:
    log = InMemoryEventLog()
    log.append_llm_response(agent_id="step:a", provider="codex", model="m",
                            raw={}, usage={}, stop_reason=None)
    log.update_last_llm_response_usage(
        agent_id="step:a", usage={"input_tokens": 5})
    resp = next(e for e in log.read() if e["type"] == "llm_response")
    assert "context_tokens" not in resp["payload"]


def test_jsonl_usage_patch_row_carries_context_tokens(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "e.jsonl")
    log.append_llm_response(agent_id="step:a", provider="codex", model="m",
                            raw={}, usage={}, stop_reason=None)
    log.update_last_llm_response_usage(
        agent_id="step:a", usage={"input_tokens": 800}, context_tokens=1000)
    patch = next(e for e in _read(tmp_path / "e.jsonl")
                 if e["type"] == "llm_response_usage_patch")
    assert patch["payload"]["context_tokens"] == 1000


def test_jsonl_usage_patch_row_omits_context_tokens_when_none(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "e.jsonl")
    log.append_llm_response(agent_id="step:a", provider="codex", model="m",
                            raw={}, usage={}, stop_reason=None)
    log.update_last_llm_response_usage(agent_id="step:a", usage={"input_tokens": 5})
    patch = next(e for e in _read(tmp_path / "e.jsonl")
                 if e["type"] == "llm_response_usage_patch")
    assert "context_tokens" not in patch["payload"]
