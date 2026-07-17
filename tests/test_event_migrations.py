from imp.engine.event_migrations import migrate_events


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
