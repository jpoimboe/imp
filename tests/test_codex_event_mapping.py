# tests/test_codex_event_mapping.py
from __future__ import annotations

import json

from imp.agents.subagents import TASK_REFUSAL_SENTINEL
from imp.engine.state import InMemoryEventLog
from imp.providers.codex_mcp import RunState, _args_fingerprint
from imp.providers.codex_runner import TASK_TURN_BOUNDARY_CLOSE, process_event
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


def test_turn_completed_clears_mint_structures() -> None:
    """AC30 (first installment): a no-match item strands a mint orphan; the
    turn.completed clear drops it so it cannot fingerprint-match a LATER turn's
    identical-args dispatch and key it to a dead event id."""
    elog = InMemoryEventLog()
    state = {"web_search_enabled": False}
    run_state = RunState(cap=10, agent_id="step:t")
    orphan_args = {"subagent_name": "impl", "task_description": "stranded"}
    run_state.minted_task_ids.append(("DEAD-MINT", _args_fingerprint(orphan_args)))
    # A real pairing also records an audit entry this turn.
    run_state.task_id_audit["sdk-x"] = "LIVE-EVENT"

    # Seed a turn item so turn.completed emits an llm_response (needed for
    # the usage-attach step to find a prior response).
    msg = FakeItemEvent(type="item.completed", item=FakeAgentMessageItem(text="work"))
    process_event(msg, elog=elog, agent_id="step:t", state=state,
                  provider="codex", model="gpt-5.5", run_state=run_state)

    done = FakeTurnCompletedEvent(usage=FakeUsage(input_tokens=1, output_tokens=1))
    process_event(done, elog=elog, agent_id="step:t", state=state,
                  provider="codex", model="gpt-5.5", run_state=run_state)

    assert run_state.minted_task_ids == []
    assert run_state.task_id_audit == {}


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


def test_refused_task_item_closes_refused_mint_keyed_to_event_id() -> None:
    """AC27/AC29: a refused dispatch's item (is_error AND text suffix-equals the
    sentinel) resolves against refused_task_mints FIRST — the tool_result keys to
    the refused mint's event id (no duplicate tool_call), and the entry clears."""
    elog = InMemoryEventLog()
    state = {"web_search_enabled": False}
    run_state = RunState(cap=10, agent_id="step:t")
    args = {"subagent_name": "impl", "task_description": "queued work"}
    run_state.refused_task_mints.append(("REFUSED-1", _args_fingerprint(args)))
    wrapped = f"Error executing tool task: {TASK_REFUSAL_SENTINEL}"

    ev = FakeItemEvent(type="item.completed", item=FakeMcpToolCallItem(
        tool="task", arguments=args, result=wrapped, status="failed",
        id="sdk-r"))
    process_event(ev, elog=elog, agent_id="step:t", state=state,
                  provider="codex", model="gpt-5.5", run_state=run_state)

    events = list(elog.read())
    calls = [e for e in events if e.get("type") == "tool_call"]
    results = [e for e in events if e.get("type") == "tool_result"]
    assert calls == []  # the minted tool_call already exists; no duplicate
    assert len(results) == 1
    assert results[0]["payload"]["id"] == "REFUSED-1"
    assert results[0]["payload"]["error"] is True
    assert run_state.refused_task_mints == []
    assert run_state.task_id_audit == {"sdk-r": "REFUSED-1"}


def test_successful_result_containing_sentinel_keeps_live_mint() -> None:
    """AC27: a SUCCESSFUL task result whose CONTENT contains the sentinel is NOT
    a refusal (is_error gate) — it pairs against the live mint, not refused."""
    elog = InMemoryEventLog()
    state = {"web_search_enabled": False}
    run_state = RunState(cap=10, agent_id="step:t")
    args = {"subagent_name": "impl", "task_description": "echo the sentinel"}
    run_state.minted_task_ids.append(("LIVE-1", _args_fingerprint(args)))
    run_state.refused_task_mints.append(
        ("REFUSED-X", _args_fingerprint(args)))

    ev = FakeItemEvent(type="item.completed", item=FakeMcpToolCallItem(
        tool="task", arguments=args,
        result=f"the subagent printed {TASK_REFUSAL_SENTINEL}",
        status="completed", id="sdk-ok"))
    process_event(ev, elog=elog, agent_id="step:t", state=state,
                  provider="codex", model="gpt-5.5", run_state=run_state)

    results = [e for e in list(elog.read()) if e.get("type") == "tool_result"]
    # Keyed to the LIVE mint (success), refused entry untouched.
    assert results[0]["payload"]["id"] == "LIVE-1"
    assert results[0]["payload"]["error"] is False
    assert run_state.minted_task_ids == []
    assert run_state.refused_task_mints == [
        ("REFUSED-X", _args_fingerprint(args))]


def test_refused_then_same_args_retry_matches_live_mint() -> None:
    """AC29: after a refused item resolves (and clears refused_task_mints), a
    same-args RETRY's successful item pairs against the freshly-minted LIVE
    entry, not the now-empty refused list."""
    elog = InMemoryEventLog()
    state = {"web_search_enabled": False}
    run_state = RunState(cap=10, agent_id="step:t")
    args = {"subagent_name": "impl", "task_description": "retry me"}
    run_state.refused_task_mints.append(("REF", _args_fingerprint(args)))
    wrapped = f"Error executing tool task: {TASK_REFUSAL_SENTINEL}"
    refused = FakeItemEvent(type="item.completed", item=FakeMcpToolCallItem(
        tool="task", arguments=args, result=wrapped, status="failed", id="s1"))
    process_event(refused, elog=elog, agent_id="step:t", state=state,
                  provider="codex", model="gpt-5.5", run_state=run_state)
    assert run_state.refused_task_mints == []

    # Retry: a fresh live mint, then a successful item.
    run_state.minted_task_ids.append(("LIVE-RETRY", _args_fingerprint(args)))
    ok = FakeItemEvent(type="item.completed", item=FakeMcpToolCallItem(
        tool="task", arguments=args, result="done", status="completed", id="s2"))
    process_event(ok, elog=elog, agent_id="step:t", state=state,
                  provider="codex", model="gpt-5.5", run_state=run_state)

    results = [e for e in list(elog.read()) if e.get("type") == "tool_result"]
    assert [r["payload"]["id"] for r in results] == ["REF", "LIVE-RETRY"]


def test_turn_boundary_synthetic_close_and_clears_when_drained() -> None:
    """AC30: at turn.completed with inflight==0, each outstanding minted AND
    refused entry gets a synthetic is_error close (the named constant), then all
    three structures clear so no orphan outlives the turn."""
    elog = InMemoryEventLog()
    state = {"web_search_enabled": False}
    run_state = RunState(cap=10, agent_id="step:t")
    run_state.minted_task_ids.append(
        ("DANGLE-M", _args_fingerprint({"subagent_name": "a"})))
    run_state.refused_task_mints.append(
        ("DANGLE-R", _args_fingerprint({"subagent_name": "b"})))
    run_state.task_id_audit["sdk"] = "LIVE"
    run_state.inflight_task_dispatches = 0

    msg = FakeItemEvent(type="item.completed", item=FakeAgentMessageItem(text="work"))
    process_event(msg, elog=elog, agent_id="step:t", state=state,
                  provider="codex", model="gpt-5.5", run_state=run_state)
    done = FakeTurnCompletedEvent(usage=FakeUsage(input_tokens=1, output_tokens=1))
    process_event(done, elog=elog, agent_id="step:t", state=state,
                  provider="codex", model="gpt-5.5", run_state=run_state)

    results = [e for e in list(elog.read()) if e.get("type") == "tool_result"]
    closed = {r["payload"]["id"]: r["payload"] for r in results
              if r["payload"]["error"]}
    assert {"DANGLE-M", "DANGLE-R"} <= set(closed)
    assert closed["DANGLE-M"]["content"] == TASK_TURN_BOUNDARY_CLOSE
    assert closed["DANGLE-R"]["content"] == TASK_TURN_BOUNDARY_CLOSE
    assert run_state.minted_task_ids == []
    assert run_state.refused_task_mints == []
    assert run_state.task_id_audit == {}


def test_turn_boundary_cleanup_deferred_while_inflight_nonzero() -> None:
    """AC30: a turn that completes with a sibling still in flight defers the
    cleanup — no synthetic close, structures intact — until a later (re-armed)
    turn.completed sees inflight==0."""
    elog = InMemoryEventLog()
    state = {"web_search_enabled": False}
    run_state = RunState(cap=10, agent_id="step:t")
    keep = ("KEEP", _args_fingerprint({"subagent_name": "a"}))
    run_state.minted_task_ids.append(keep)
    run_state.inflight_task_dispatches = 1  # a sibling is still draining

    msg = FakeItemEvent(type="item.completed", item=FakeAgentMessageItem(text="work"))
    process_event(msg, elog=elog, agent_id="step:t", state=state,
                  provider="codex", model="gpt-5.5", run_state=run_state)
    done = FakeTurnCompletedEvent(usage=FakeUsage(input_tokens=1, output_tokens=1))
    process_event(done, elog=elog, agent_id="step:t", state=state,
                  provider="codex", model="gpt-5.5", run_state=run_state)

    results = [e for e in list(elog.read()) if e.get("type") == "tool_result"]
    assert all(r["payload"]["id"] != "KEEP" for r in results)  # no close yet
    assert run_state.minted_task_ids == [keep]  # not cleared
