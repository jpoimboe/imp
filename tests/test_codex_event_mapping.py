# tests/test_codex_event_mapping.py
from __future__ import annotations

import json

from imp.engine.state import InMemoryEventLog
from imp.providers.codex_mcp import RunState, _args_fingerprint
from imp.providers.codex_runner import process_event
from tests.codex_fakes import (
    FakeAgentMessageItem,
    FakeItemEvent,
    FakeMcpToolCallItem,
    FakeTurnCompletedEvent,
    FakeUsage,
)


def test_task_completion_rekeys_to_minted_id_by_fingerprint() -> None:
    elog = InMemoryEventLog()
    state = {"web_search_enabled": False}
    run_state = RunState(cap=10, agent_id="step:t")
    args = {"subagent_name": "impl", "task_description": "do it"}
    # as if dispatch_tool minted it (event id, fingerprint)
    run_state.minted_task_ids.append(("MINTED-1", _args_fingerprint(args)))

    ev = FakeItemEvent(type="item.completed", item=FakeMcpToolCallItem(
        tool="task", arguments=args, result='{"answer": "ok"}', id="sdk-item-9"))

    process_event(ev, elog=elog, agent_id="step:t", state=state,
                  provider="codex", model="gpt-5.5", run_state=run_state)

    events = list(elog.read())
    calls = [e for e in events if e.get("type") == "tool_call"]
    results = [e for e in events if e.get("type") == "tool_result"]
    # No duplicate tool_call for task (dispatch_tool already minted it).
    assert calls == []
    # tool_result is keyed to the MINTED id, not the SDK item id.
    assert len(results) == 1
    assert results[0]["payload"]["id"] == "MINTED-1"
    # The entry was match-and-removed and the sdk->event mapping recorded.
    assert run_state.minted_task_ids == []
    assert run_state.task_id_audit == {"sdk-item-9": "MINTED-1"}


def test_two_task_calls_pair_by_fingerprint_out_of_order() -> None:
    """AC24: concurrent completions arrive in ARBITRARY order; each result keys to
    the mint whose args fingerprint it matches, not to FIFO position."""
    elog = InMemoryEventLog()
    state = {"web_search_enabled": False}
    run_state = RunState(cap=10, agent_id="step:t")
    args_a = {"subagent_name": "impl", "task_description": "task A"}
    args_b = {"subagent_name": "impl", "task_description": "task B"}
    run_state.minted_task_ids.append(("MINT-A", _args_fingerprint(args_a)))
    run_state.minted_task_ids.append(("MINT-B", _args_fingerprint(args_b)))

    # B completes FIRST (reverse of dispatch order).
    for sdk_id, item_args in (("sdk-b", args_b), ("sdk-a", args_a)):
        ev = FakeItemEvent(type="item.completed", item=FakeMcpToolCallItem(
            tool="task", arguments=item_args, result="ok", id=sdk_id))
        process_event(ev, elog=elog, agent_id="step:t", state=state,
                      provider="codex", model="gpt-5.5", run_state=run_state)

    results = [e for e in list(elog.read()) if e.get("type") == "tool_result"]
    # B's result keyed to MINT-B though it arrived first; A's to MINT-A.
    assert [r["payload"]["id"] for r in results] == ["MINT-B", "MINT-A"]
    assert run_state.minted_task_ids == []


def test_cap_tripped_distinct_args_item_does_not_steal_live_mint() -> None:
    """AC24: a cap-tripped task returns before minting, so its item arrives
    unminted. With distinct args it finds no fingerprint match and takes the
    no-mint sdk-keyed path — the live sibling's mint is untouched (no FIFO steal)."""
    elog = InMemoryEventLog()
    state = {"web_search_enabled": False}
    run_state = RunState(cap=10, agent_id="step:t")
    live = {"subagent_name": "impl", "task_description": "real work"}
    run_state.minted_task_ids.append(("LIVE-MINT", _args_fingerprint(live)))

    capped = {"subagent_name": "impl", "task_description": "over the cap"}
    ev = FakeItemEvent(type="item.completed", item=FakeMcpToolCallItem(
        tool="task", arguments=capped, result="tool_call_ceiling_exceeded",
        id="sdk-capped"))
    process_event(ev, elog=elog, agent_id="step:t", state=state,
                  provider="codex", model="gpt-5.5", run_state=run_state)

    calls = [e for e in list(elog.read()) if e.get("type") == "tool_call"]
    results = [e for e in list(elog.read()) if e.get("type") == "tool_result"]
    # The unminted item emits its OWN sdk-keyed pair.
    assert [c["payload"]["id"] for c in calls] == ["sdk-capped"]
    assert [r["payload"]["id"] for r in results] == ["sdk-capped"]
    # The live sibling's mint is untouched.
    assert run_state.minted_task_ids == [("LIVE-MINT", _args_fingerprint(live))]


def test_string_shaped_arguments_parsed_then_fingerprint_matches() -> None:
    """AC24: some SDK/MCP shapes deliver `arguments` as a JSON string — it is
    parsed to a dict before fingerprinting so the match still pairs."""
    elog = InMemoryEventLog()
    state = {"web_search_enabled": False}
    run_state = RunState(cap=10, agent_id="step:t")
    args = {"subagent_name": "impl", "task_description": "do it"}
    run_state.minted_task_ids.append(("MINTED-S", _args_fingerprint(args)))

    ev = FakeItemEvent(type="item.completed", item=FakeMcpToolCallItem(
        tool="task", arguments=json.dumps(args), result="ok", id="sdk-s"))
    process_event(ev, elog=elog, agent_id="step:t", state=state,
                  provider="codex", model="gpt-5.5", run_state=run_state)

    results = [e for e in list(elog.read()) if e.get("type") == "tool_result"]
    assert results[0]["payload"]["id"] == "MINTED-S"
    assert run_state.minted_task_ids == []


def test_unparseable_string_arguments_degrade_to_no_match() -> None:
    """AC24: an unparseable string `arguments` must NOT raise and must NOT match
    an empty-args mint — it degrades to the no-mint sdk-keyed path."""
    elog = InMemoryEventLog()
    state = {"web_search_enabled": False}
    run_state = RunState(cap=10, agent_id="step:t")
    run_state.minted_task_ids.append(("MINT-EMPTY", _args_fingerprint({})))

    ev = FakeItemEvent(type="item.completed", item=FakeMcpToolCallItem(
        tool="task", arguments="{not json", result="ok", id="sdk-bad"))
    process_event(ev, elog=elog, agent_id="step:t", state=state,
                  provider="codex", model="gpt-5.5", run_state=run_state)

    calls = [e for e in list(elog.read()) if e.get("type") == "tool_call"]
    # No match → sdk-keyed pair; the empty-args mint is untouched.
    assert [c["payload"]["id"] for c in calls] == ["sdk-bad"]
    assert run_state.minted_task_ids == [("MINT-EMPTY", _args_fingerprint({}))]


def test_turn_completed_emits_reassembled_llm_response_before_usage() -> None:
    elog = InMemoryEventLog()
    state = {"web_search_enabled": False}
    run_state = RunState(cap=10, agent_id="step:t")

    # agent_message then a tool call, then turn.completed with usage.
    msg = FakeItemEvent(type="item.completed", item=FakeAgentMessageItem(text="hello"))
    call = FakeItemEvent(type="item.completed", item=FakeMcpToolCallItem(
        tool="write_file", arguments={"path": "x"}, result="ok", id="c1"))
    done = FakeTurnCompletedEvent(usage=FakeUsage(input_tokens=10, output_tokens=5))

    for ev in (msg, call, done):
        process_event(ev, elog=elog, agent_id="step:t", state=state,
                      provider="codex", model="gpt-5.5", run_state=run_state)

    llm = [e for e in list(elog.read()) if e.get("type") == "llm_response"]
    assert len(llm) == 1  # ONE full-turn response, not per-agent_message
    content = llm[0]["payload"]["raw"]["content"]
    kinds = [b["type"] for b in content]
    assert "text" in kinds and "tool_use" in kinds  # carries tool_use now
    # Usage attached to that same response.
    assert llm[0]["payload"]["usage"]  # non-empty
