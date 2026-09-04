import copy
import re

from imp.engine.event_migrations import migrate_events, migrate_id_ns_event_to_block


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
