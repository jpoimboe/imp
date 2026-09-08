import copy
import re

from imp.engine.event_migrations import (
    backfill_query_result_session_ids,
    backfill_step_round,
    migrate_events,
    migrate_id_ns_event_to_block,
)


def test_llm_response_renamed_to_agent_message():
    old = [{"id": "r1", "parent_id": "u1", "agent_id": "A", "timestamp": "T",
            "type": "llm_response",
            "payload": {"provider": "anthropic", "model": "m", "raw": {"x": 1},
                        "usage": {"input_tokens": 5}, "stop_reason": "end_turn"}}]
    out = migrate_events(old)
    assert out == [{"id": "r1", "parent_id": "u1", "agent_id": "A", "timestamp": "T",
                    "type": "agent_message",
                    "payload": {"provider": "anthropic", "model": "m", "raw": {"x": 1},
                                "usage": {"input_tokens": 5}, "stop_reason": "end_turn"}}]


def test_usage_patch_renamed_and_stays_bare():
    old = [{"type": "llm_response_usage_patch", "agent_id": "A",
            "payload": {"usage": {"input_tokens": 5, "output_tokens": 2}}}]
    out = migrate_events(old)
    assert out == [{"type": "agent_message_usage_patch", "agent_id": "A",
                    "payload": {"usage": {"input_tokens": 5, "output_tokens": 2}}}]
    assert "id" not in out[0]
    assert "parent_id" not in out[0]
    assert "timestamp" not in out[0]


def test_agent_output_renamed_to_agent_result():
    old = [{"id": "o1", "parent_id": "u5", "agent_id": "A", "timestamp": "T",
            "type": "agent_output", "payload": {"status": "Success", "outcome": "done"}}]
    out = migrate_events(old)
    assert out == [{"id": "o1", "parent_id": "u5", "agent_id": "A", "timestamp": "T",
                    "type": "agent_result",
                    "payload": {"status": "Success", "outcome": "done"}}]


def test_opening_intent_becomes_intent_user_message():
    old = [{"id": "i0", "parent_id": None, "agent_id": None, "timestamp": "T",
            "type": "opening_intent", "payload": {"text": "Fix the bug"}}]
    out = migrate_events(old)
    assert out == [{"id": "i0", "parent_id": None, "agent_id": None, "timestamp": "T",
                    "type": "user_message",
                    "payload": {"role": "user",
                                "content": [{"type": "text", "text": "Fix the bug"}],
                                "subtype": "intent"}}]


def test_engine_continuation_becomes_continuation_engine_message():
    old = [{"id": "e1", "parent_id": "u4", "agent_id": "A", "timestamp": "T",
            "type": "engine_continuation", "payload": {"text": "Please continue"}}]
    out = migrate_events(old)
    assert out == [{"id": "e1", "parent_id": "u4", "agent_id": "A", "timestamp": "T",
                    "type": "engine_message",
                    "payload": {"role": "user",
                                "content": [{"type": "text", "text": "Please continue"}],
                                "subtype": "continuation"}}]


def test_unknown_control_event_passes_through_unchanged():
    old = [{"id": "t1", "parent_id": "o1", "agent_id": "A", "timestamp": "T",
            "type": "tool_call", "payload": {"id": "tc", "name": "read", "input": {}}}]
    out = migrate_events(old)
    assert out == old


def test_step_context_strips_system_prompt_and_synthesizes_engine_message():
    old = [{"id": "c1", "parent_id": None, "agent_id": "A", "timestamp": "T1",
            "type": "step_context",
            "payload": {"provider": "anthropic", "model": "m",
                        "system_prompt": "You are a coder",
                        "tools_schema": [], "params": {}, "max_tool_calls": 50}}]
    out = migrate_events(old)
    assert len(out) == 2
    assert out[0] == {"id": "c1", "parent_id": None, "agent_id": "A", "timestamp": "T1",
                      "type": "step_context",
                      "payload": {"provider": "anthropic", "model": "m",
                                  "tools_schema": [], "params": {},
                                  "max_tool_calls": 50}}
    assert list(out[0]["payload"]) == ["provider", "model", "tools_schema",
                                       "params", "max_tool_calls"]
    syn = out[1]
    assert syn["type"] == "engine_message"
    assert syn["parent_id"] == "c1"
    assert syn["agent_id"] == "A"
    assert syn["timestamp"] == "T1"
    assert re.fullmatch(r"[0-9a-f]{32}", syn["id"])
    assert syn["payload"] == {"role": "system",
                              "content": [{"type": "text", "text": "You are a coder"}],
                              "subtype": "system_prompt"}


def test_step_context_without_system_prompt_passes_through_unchanged():
    old = [{"id": "c1", "parent_id": None, "agent_id": "A", "timestamp": "T1",
            "type": "step_context",
            "payload": {"provider": "anthropic", "model": "m",
                        "tools_schema": [], "params": {}, "max_tool_calls": 50}}]
    out = migrate_events(old)
    assert out == old


def test_human_true_reply():
    old = [{"id": "u2", "parent_id": "a1", "agent_id": "A", "timestamp": "T",
            "type": "user_message",
            "payload": {"role": "user", "content": [{"type": "text", "text": "go on"}],
                        "human": True}}]
    out = migrate_events(old)
    assert out == [{"id": "u2", "parent_id": "a1", "agent_id": "A", "timestamp": "T",
                    "type": "user_message",
                    "payload": {"role": "user",
                                "content": [{"type": "text", "text": "go on"}],
                                "subtype": "reply"}}]


def test_human_true_interject_drops_following_user_interjected():
    old = [
        {"id": "u3", "parent_id": "u2", "agent_id": "A", "timestamp": "T1",
         "type": "user_message",
         "payload": {"role": "user", "content": [{"type": "text", "text": "stop"}],
                     "human": True}},
        {"id": "j1", "parent_id": "u3", "agent_id": "A", "timestamp": "T2",
         "type": "user_interjected", "payload": {}},
    ]
    out = migrate_events(old)
    assert out == [{"id": "u3", "parent_id": "u2", "agent_id": "A", "timestamp": "T1",
                    "type": "user_message",
                    "payload": {"role": "user",
                                "content": [{"type": "text", "text": "stop"}],
                                "subtype": "interject"}}]


def test_engine_injected_first_after_step_context_is_agent_prompt():
    old = [
        {"id": "c1", "parent_id": None, "agent_id": "A", "timestamp": "T1",
         "type": "step_context",
         "payload": {"provider": "p", "model": "m", "tools_schema": [],
                     "params": {}, "max_tool_calls": 1}},
        {"id": "u1", "parent_id": "c1", "agent_id": "A", "timestamp": "T2",
         "type": "user_message",
         "payload": {"role": "user", "content": [{"type": "text", "text": "begin"}],
                     "human": False}},
    ]
    out = migrate_events(old)
    assert out[1] == {"id": "u1", "parent_id": "c1", "agent_id": "A", "timestamp": "T2",
                      "type": "engine_message",
                      "payload": {"role": "user",
                                  "content": [{"type": "text", "text": "begin"}],
                                  "subtype": "agent_prompt"}}


def test_engine_injected_without_step_context_is_continuation():
    old = [{"id": "u5", "parent_id": "e1", "agent_id": "A", "timestamp": "T",
            "type": "user_message",
            "payload": {"role": "user", "content": [{"type": "text", "text": "resume"}],
                        "human": False}}]
    out = migrate_events(old)
    assert out[0]["type"] == "engine_message"
    assert out[0]["payload"]["subtype"] == "continuation"
    assert "human" not in out[0]["payload"]


def test_completion_nudge_collapses_with_following_injection():
    old = [
        {"id": "n1", "parent_id": "u3", "agent_id": "A", "timestamp": "T1",
         "type": "completion_nudge", "payload": {"message": "Your turn ended"}},
        {"id": "u4", "parent_id": "n1", "agent_id": "A", "timestamp": "T2",
         "type": "user_message",
         "payload": {"role": "user", "content": [{"type": "text", "text": "x"}],
                     "human": False}},
    ]
    out = migrate_events(old)
    assert out == [{"id": "n1", "parent_id": "u3", "agent_id": "A", "timestamp": "T1",
                    "type": "engine_message",
                    "payload": {"role": "user",
                                "content": [{"type": "text", "text": "Your turn ended"}],
                                "subtype": "completion"}}]


def test_new_format_user_message_passes_through_unchanged():
    new = [{"id": "u2", "parent_id": "a1", "agent_id": "A", "timestamp": "T",
            "type": "user_message",
            "payload": {"role": "user", "content": [{"type": "text", "text": "hi"}],
                        "subtype": "reply"}}]
    out = migrate_events(new)
    assert out == new


def test_completion_nudge_suppression_is_per_agent():
    old = [
        {"id": "n1", "parent_id": "u3", "agent_id": "A", "timestamp": "T1",
         "type": "completion_nudge", "payload": {"message": "ended"}},
        {"id": "u1", "parent_id": "x", "agent_id": "B", "timestamp": "T2",
         "type": "user_message",
         "payload": {"role": "user", "content": [{"type": "text", "text": "b-msg"}],
                     "human": False}},
    ]
    out = migrate_events(old)
    assert len(out) == 2
    assert out[0]["type"] == "engine_message"
    assert out[0]["payload"]["subtype"] == "completion"
    assert out[1] == {"id": "u1", "parent_id": "x", "agent_id": "B", "timestamp": "T2",
                      "type": "engine_message",
                      "payload": {"role": "user",
                                  "content": [{"type": "text", "text": "b-msg"}],
                                  "subtype": "continuation"}}


def test_agent_prompt_pending_is_per_agent():
    old = [
        {"id": "c1", "parent_id": None, "agent_id": "A", "timestamp": "T1",
         "type": "step_context",
         "payload": {"provider": "p", "model": "m", "tools_schema": [],
                     "params": {}, "max_tool_calls": 1}},
        {"id": "u1", "parent_id": "c1", "agent_id": "B", "timestamp": "T2",
         "type": "user_message",
         "payload": {"role": "user", "content": [{"type": "text", "text": "b-begin"}],
                     "human": False}},
    ]
    out = migrate_events(old)
    assert len(out) == 2
    assert out[1] == {"id": "u1", "parent_id": "c1", "agent_id": "B", "timestamp": "T2",
                      "type": "engine_message",
                      "payload": {"role": "user",
                                  "content": [{"type": "text", "text": "b-begin"}],
                                  "subtype": "continuation"}}


def _old_full():
    return [
        {"id": "i0", "parent_id": None, "agent_id": None, "timestamp": "T0",
         "type": "opening_intent", "payload": {"text": "Fix the bug"}},
        {"id": "c1", "parent_id": None, "agent_id": "A", "timestamp": "T1",
         "type": "step_context",
         "payload": {"provider": "anthropic", "model": "m",
                     "system_prompt": "You are a coder",
                     "tools_schema": [], "params": {}, "max_tool_calls": 50}},
        {"id": "u1", "parent_id": "c1", "agent_id": "A", "timestamp": "T2",
         "type": "user_message",
         "payload": {"role": "user", "content": [{"type": "text", "text": "begin"}],
                     "human": False}},
        {"id": "r1", "parent_id": "u1", "agent_id": "A", "timestamp": "T3",
         "type": "llm_response",
         "payload": {"provider": "anthropic", "model": "m", "raw": {"x": 1},
                     "usage": {"input_tokens": 5}, "stop_reason": "end_turn"}},
        {"type": "llm_response_usage_patch", "agent_id": "A",
         "payload": {"usage": {"input_tokens": 5, "output_tokens": 2}}},
        {"id": "a1", "parent_id": "r1", "agent_id": "A", "timestamp": "T4",
         "type": "awaiting_user_input", "payload": {}},
        {"id": "u2", "parent_id": "a1", "agent_id": "A", "timestamp": "T5",
         "type": "user_message",
         "payload": {"role": "user", "content": [{"type": "text", "text": "go on"}],
                     "human": True}},
        {"id": "u3", "parent_id": "u2", "agent_id": "A", "timestamp": "T6",
         "type": "user_message",
         "payload": {"role": "user", "content": [{"type": "text", "text": "stop"}],
                     "human": True}},
        {"id": "j1", "parent_id": "u3", "agent_id": "A", "timestamp": "T7",
         "type": "user_interjected", "payload": {}},
        {"id": "n1", "parent_id": "u3", "agent_id": "A", "timestamp": "T8",
         "type": "completion_nudge", "payload": {"message": "Your turn ended"}},
        {"id": "u4", "parent_id": "n1", "agent_id": "A", "timestamp": "T9",
         "type": "user_message",
         "payload": {"role": "user", "content": [{"type": "text", "text": "nudge"}],
                     "human": False}},
        {"id": "e1", "parent_id": "u4", "agent_id": "A", "timestamp": "T10",
         "type": "engine_continuation", "payload": {"text": "Please continue"}},
        {"id": "u5", "parent_id": "e1", "agent_id": "A", "timestamp": "T11",
         "type": "user_message",
         "payload": {"role": "user", "content": [{"type": "text", "text": "resume"}],
                     "human": False}},
        {"id": "o1", "parent_id": "u5", "agent_id": "A", "timestamp": "T12",
         "type": "agent_output", "payload": {"status": "Success", "outcome": "done"}},
        {"id": "t1", "parent_id": "o1", "agent_id": "A", "timestamp": "T13",
         "type": "tool_call", "payload": {"id": "tc", "name": "read", "input": {}}},
        {"id": "s1", "parent_id": "t1", "agent_id": "A", "timestamp": "T14",
         "type": "skill_load", "payload": {"name": "x", "source": "y"}},
    ]


def _expected_full(sys_id):
    return [
        {"id": "i0", "parent_id": None, "agent_id": None, "timestamp": "T0",
         "type": "user_message",
         "payload": {"role": "user",
                     "content": [{"type": "text", "text": "Fix the bug"}],
                     "subtype": "intent"}},
        {"id": "c1", "parent_id": None, "agent_id": "A", "timestamp": "T1",
         "type": "step_context",
         "payload": {"provider": "anthropic", "model": "m",
                     "tools_schema": [], "params": {}, "max_tool_calls": 50}},
        {"id": sys_id, "parent_id": "c1", "agent_id": "A", "timestamp": "T1",
         "type": "engine_message",
         "payload": {"role": "system",
                     "content": [{"type": "text", "text": "You are a coder"}],
                     "subtype": "system_prompt"}},
        {"id": "u1", "parent_id": "c1", "agent_id": "A", "timestamp": "T2",
         "type": "engine_message",
         "payload": {"role": "user", "content": [{"type": "text", "text": "begin"}],
                     "subtype": "agent_prompt"}},
        {"id": "r1", "parent_id": "u1", "agent_id": "A", "timestamp": "T3",
         "type": "agent_message",
         "payload": {"provider": "anthropic", "model": "m", "raw": {"x": 1},
                     "usage": {"input_tokens": 5}, "stop_reason": "end_turn"}},
        {"type": "agent_message_usage_patch", "agent_id": "A",
         "payload": {"usage": {"input_tokens": 5, "output_tokens": 2}}},
        {"id": "a1", "parent_id": "r1", "agent_id": "A", "timestamp": "T4",
         "type": "awaiting_user_input", "payload": {}},
        {"id": "u2", "parent_id": "a1", "agent_id": "A", "timestamp": "T5",
         "type": "user_message",
         "payload": {"role": "user", "content": [{"type": "text", "text": "go on"}],
                     "subtype": "reply"}},
        {"id": "u3", "parent_id": "u2", "agent_id": "A", "timestamp": "T6",
         "type": "user_message",
         "payload": {"role": "user", "content": [{"type": "text", "text": "stop"}],
                     "subtype": "interject"}},
        {"id": "n1", "parent_id": "u3", "agent_id": "A", "timestamp": "T8",
         "type": "engine_message",
         "payload": {"role": "user",
                     "content": [{"type": "text", "text": "Your turn ended"}],
                     "subtype": "completion"}},
        {"id": "e1", "parent_id": "u4", "agent_id": "A", "timestamp": "T10",
         "type": "engine_message",
         "payload": {"role": "user",
                     "content": [{"type": "text", "text": "Please continue"}],
                     "subtype": "continuation"}},
        {"id": "u5", "parent_id": "e1", "agent_id": "A", "timestamp": "T11",
         "type": "engine_message",
         "payload": {"role": "user", "content": [{"type": "text", "text": "resume"}],
                     "subtype": "continuation"}},
        {"id": "o1", "parent_id": "u5", "agent_id": "A", "timestamp": "T12",
         "type": "agent_result", "payload": {"status": "Success", "outcome": "done"}},
        {"id": "t1", "parent_id": "o1", "agent_id": "A", "timestamp": "T13",
         "type": "tool_call", "payload": {"id": "tc", "name": "read", "input": {}}},
        {"id": "s1", "parent_id": "t1", "agent_id": "A", "timestamp": "T14",
         "type": "skill_load", "payload": {"name": "x", "source": "y"}},
    ]


def test_golden_fixture_full_mapping():
    out = migrate_events(_old_full())
    syn = [e for e in out
           if e["type"] == "engine_message"
           and e["payload"]["subtype"] == "system_prompt"]
    assert len(syn) == 1
    assert re.fullmatch(r"[0-9a-f]{32}", syn[0]["id"])
    assert out == _expected_full(syn[0]["id"])


def test_idempotent_double_migration():
    once = migrate_events(_old_full())
    assert migrate_events(copy.deepcopy(once)) == once


def test_already_new_format_returns_unchanged():
    new = _expected_full("a" * 32)
    assert migrate_events(new) == new


def test_does_not_mutate_input():
    old = _old_full()
    snapshot = copy.deepcopy(old)
    migrate_events(old)
    assert old == snapshot


def _codex_pair():
    """A stored codex sub_agent fold as it was written before the id_ns fix: an
    UNSTAMPED tool_call keyed on the SDK item id, and its id_ns="event" result."""
    return [
        {"id": "e1", "parent_id": None, "agent_id": "A", "timestamp": "T1",
         "type": "tool_call",
         "payload": {"id": "item_1", "name": "sub_agent",
                     "input": {"subagent_name": "explorer", "task_description": "x"}}},
        {"id": "e2", "parent_id": "e1", "agent_id": "A", "timestamp": "T2",
         "type": "tool_result",
         "payload": {"id": "item_1", "id_ns": "event", "content": "done",
                     "error": False}},
    ]


def _legacy_unstamped_pair():
    """A pre-id_ns log: neither side carries the marker. It must stay that way so the
    renderer's `id_ns is None` ordered probe keeps closing it."""
    return [
        {"id": "e3", "parent_id": None, "agent_id": "A", "timestamp": "T3",
         "type": "tool_call",
         "payload": {"id": "b1", "name": "sub_agent",
                     "input": {"subagent_name": "reviewer", "task_description": "go"}}},
        {"id": "e4", "parent_id": "e3", "agent_id": "A", "timestamp": "T4",
         "type": "tool_result",
         "payload": {"id": "b1", "content": "ok", "error": False}},
    ]


def test_restamps_both_sides_of_an_event_keyed_codex_fold():
    out = migrate_id_ns_event_to_block(_codex_pair())
    assert [e["payload"].get("id_ns") for e in out] == ["block", "block"]
    assert [e["payload"]["id"] for e in out] == ["item_1", "item_1"]
    assert out[1]["payload"]["content"] == "done"      # rest of the payload survives
    assert out[0]["type"] == "tool_call" and out[1]["type"] == "tool_result"


def test_legacy_unstamped_pair_is_returned_unchanged():
    legacy = _legacy_unstamped_pair()
    assert migrate_id_ns_event_to_block(legacy) == _legacy_unstamped_pair()


def test_second_run_yields_output_identical_to_the_first():
    once = migrate_id_ns_event_to_block(_codex_pair())
    assert migrate_id_ns_event_to_block(copy.deepcopy(once)) == once


def test_unstamped_call_is_not_restamped_without_a_matching_event_result():
    # Only the tool_call whose id appears among the id_ns="event" RESULT ids is
    # restamped; an unrelated unstamped dispatch in the same log is left alone.
    events = _codex_pair() + _legacy_unstamped_pair()
    out = migrate_id_ns_event_to_block(events)
    assert [e["payload"].get("id_ns") for e in out] == ["block", "block", None, None]


def test_does_not_mutate_input_events():
    # Load-bearing: the archive pass detects "blob changed" with `migrated != events`,
    # which an in-place mutation would defeat.
    events = _codex_pair()
    snapshot = copy.deepcopy(events)
    migrate_id_ns_event_to_block(events)
    assert events == snapshot


_A = "step:build/coder"


def _qr(cost, agent_id=_A, **payload):
    return {"id": f"q{cost}", "parent_id": None, "agent_id": agent_id,
            "timestamp": "T", "type": "query_result",
            "payload": {"total_cost_usd": cost, **payload}}


def _step_started(phase="build", step="coder", round_=None, eid="s1"):
    payload = {"phase": phase, "step": step, "model": "m",
               "provider": "claude_code"}
    if round_ is not None:
        payload["round"] = round_
    return {"id": eid, "parent_id": None, "agent_id": None, "timestamp": "T",
            "type": "step_started", "payload": payload}


def _last_per_session(events):
    """What the accounting path does with the result: rows are cumulative inside a
    session, so each session contributes only its last row."""
    last = {}
    for e in events:
        if e["type"] == "query_result":
            last[e["payload"]["session_id"]] = e["payload"]["total_cost_usd"]
    return round(sum(last.values()), 2)


def test_a_step_started_for_that_agent_closes_the_cumulative_series():
    # The hava67 shape in miniature: one agent, two SDK sessions, every row
    # cumulative within its own. Adding all five is the live over-report
    # (measured $45.01 against a true $28.11); the last row of each session is
    # the truth. A partition that lumped all five under one id would total 6.40.
    events = [_qr(3.00), _qr(8.00), _qr(12.50), _step_started(),
              _qr(4.00), _qr(6.40)]
    out = backfill_query_result_session_ids(events)
    ids = [e["payload"]["session_id"] for e in out if e["type"] == "query_result"]
    assert ids[0] == ids[1] == ids[2]
    assert ids[3] == ids[4]
    assert ids[0] != ids[3]
    assert _last_per_session(out) == 18.90        # naive sum would be 33.90


def test_a_cost_drop_alone_opens_a_new_session():
    # A subagent gets no step_started of its own, and the fresh_stream path skips
    # step_context, so a cumulative total that goes DOWN is the only boundary
    # signal left.
    events = [_qr(5.00, agent_id="sub:a1"), _qr(9.00, agent_id="sub:a1"),
              _qr(2.00, agent_id="sub:a1"), _qr(7.50, agent_id="sub:a1")]
    out = backfill_query_result_session_ids(events)
    ids = [e["payload"]["session_id"] for e in out]
    assert ids[0] == ids[1]
    assert ids[2] == ids[3]
    assert ids[0] != ids[2]
    assert _last_per_session(out) == 16.50        # naive sum would be 23.50


def test_a_step_started_for_another_agent_does_not_close_this_series():
    # Resetting on ANY step_started would split this agent's one cumulative series
    # in two and double-count its first 1.00.
    events = [_qr(1.00), _step_started(phase="scoping", step="threshold"),
              _qr(4.00)]
    out = backfill_query_result_session_ids(events)
    ids = [e["payload"]["session_id"] for e in out if e["type"] == "query_result"]
    assert ids[0] == ids[1]
    assert _last_per_session(out) == 4.00


def test_two_agents_interleaved_keep_separate_series():
    events = [_qr(1.00), _qr(0.50, agent_id="sub:x"),
              _qr(2.00), _qr(0.90, agent_id="sub:x")]
    out = backfill_query_result_session_ids(events)
    ids = [e["payload"]["session_id"] for e in out]
    assert ids[0] == ids[2]
    assert ids[1] == ids[3]
    assert ids[0] != ids[1]
    assert _last_per_session(out) == 2.90


def test_a_row_with_no_cost_reading_stays_in_the_current_session():
    # Reading an absent cost as 0.0 would look like a drop and split the series,
    # re-adding the 3.00 already contained in the 8.00.
    events = [_qr(3.00), _qr(None), _qr(8.00)]
    out = backfill_query_result_session_ids(events)
    ids = [e["payload"]["session_id"] for e in out]
    assert ids[0] == ids[1] == ids[2]
    assert _last_per_session(out) == 8.00


def test_a_row_that_already_carries_an_id_is_left_alone():
    events = [_qr(2.00, session_id="live-abc"), _qr(5.00, session_id="live-abc")]
    assert backfill_query_result_session_ids(events) == events


def test_a_second_backfill_run_yields_identical_output():
    # Named for this transform: the file already carries an id_ns-flavoured
    # `test_second_run_yields_output_identical_to_the_first` at :409.
    once = backfill_query_result_session_ids(
        [_qr(3.00), _qr(8.00), _step_started(), _qr(4.00)])
    assert backfill_query_result_session_ids(copy.deepcopy(once)) == once


def test_passes_other_events_through_and_does_not_mutate_the_input():
    # Load-bearing: both migration paths detect "changed" with `migrated != events`,
    # which an in-place mutation would defeat.
    events = [_step_started(),
              {"id": "t1", "parent_id": None, "agent_id": _A, "timestamp": "T",
               "type": "tool_call", "payload": {"name": "bash", "id": "c1"}},
              _qr(1.00)]
    snapshot = copy.deepcopy(events)
    out = backfill_query_result_session_ids(events)
    assert events == snapshot
    assert out[0] == snapshot[0]
    assert out[1] == snapshot[1]
    assert set(out[2]["payload"]) == {"session_id", "total_cost_usd"}
    assert out[2]["payload"]["total_cost_usd"] == 1.00
    assert out[2]["payload"]["session_id"]


def test_every_unstamped_step_started_including_loop_repeats_gets_round_one():
    # A looping step's repeat entries are separate rows; each one is stamped.
    out = backfill_step_round([_step_started(step="reviewer", eid="s1"), _qr(1.00),
                               _step_started(step="reviewer", eid="s2")])
    assert [e["payload"]["round"]
            for e in out if e["type"] == "step_started"] == [1, 1]


def test_other_payload_keys_and_other_events_are_left_alone():
    # Load-bearing: both callers detect "changed" with `migrated != events`, which
    # an in-place mutation would defeat.
    events = [_step_started(), _qr(2.00, session_id="s1")]
    snapshot = copy.deepcopy(events)
    out = backfill_step_round(events)
    assert events == snapshot
    assert out[1] == snapshot[1]
    assert out[0]["payload"] == {**snapshot[0]["payload"], "round": 1}


def test_an_already_stamped_round_is_passed_through_not_reset():
    # The chain re-runs over post-change blobs; an unconditional stamp would erase
    # the real rounds the live engine wrote.
    events = [_step_started(round_=3)]
    assert backfill_step_round(events) == events


def test_a_second_run_over_a_mixed_stream_yields_the_first_run():
    once = backfill_step_round(
        [_step_started(round_=3, eid="s1"), _step_started(eid="s2"), _qr(1.00)])
    assert backfill_step_round(copy.deepcopy(once)) == once
