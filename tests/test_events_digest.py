# tests/test_events_digest.py
from __future__ import annotations

import json

from imp.engine.events_digest import _FULL_CAP, _TRUNC_MARK, digest_event, full_line


def test_digest_event_is_single_line() -> None:
    ev = {
        "id": "e1", "parent_id": None, "agent_id": "step:plan/plan-writer",
        "timestamp": "2026-07-01T00:00:00+00:00", "type": "awaiting_user_input",
        "payload": {"prompt": "line one\nline two\nline three"},
    }
    line = digest_event(ev)
    assert "\n" not in line
    # Envelope carries type + agent_id so a reader can see whose provenance this is.
    assert "awaiting_user_input" in line
    assert "step:plan/plan-writer" in line
    assert "line one" in line


def test_step_context_digest_drops_prompt_and_schema() -> None:
    ev = {
        "id": "e2", "parent_id": None, "agent_id": "step:plan/plan-writer",
        "timestamp": "2026-07-01T00:00:01+00:00", "type": "step_context",
        "payload": {
            "provider": "anthropic", "model": "claude-x",
            "system_prompt": "SECRET_SYSTEM_PROMPT_BODY",
            "tools_schema": [{"name": "SECRET_TOOL_SCHEMA"}],
            "params": {}, "max_tool_calls": 40,
        },
    }
    line = digest_event(ev)
    assert "claude-x" in line
    assert "SECRET_SYSTEM_PROMPT_BODY" not in line
    assert "SECRET_TOOL_SCHEMA" not in line


def test_llm_response_digest_shows_stop_tools_usage_and_text() -> None:
    ev = {
        "id": "e3", "parent_id": None, "agent_id": "step:x/y",
        "timestamp": "2026-07-01T00:00:02+00:00", "type": "llm_response",
        "payload": {
            "provider": "anthropic", "model": "claude-x",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 111, "output_tokens": 22},
            "raw": {"content": [
                {"type": "text", "text": "here is my analysis"},
                {"type": "tool_use", "id": "t1", "name": "read_file", "input": {}},
            ]},
        },
    }
    line = digest_event(ev)
    assert "\n" not in line
    assert "tool_use" in line          # stop_reason
    assert "111" in line               # usage token count
    assert "here is my analysis" in line
    assert "tools=1" in line           # one tool_use counted


def test_tool_result_digest_shows_error_and_byte_size() -> None:
    ev = {
        "id": "e4", "parent_id": None, "agent_id": "step:x/y",
        "timestamp": "2026-07-01T00:00:03+00:00", "type": "tool_result",
        "payload": {"id": "t1", "content": "ABCDEFGHIJ", "error": True},
    }
    line = digest_event(ev)
    assert "error=True" in line
    assert "bytes=10" in line          # byte size of "ABCDEFGHIJ"


def test_unknown_type_uses_generic_scalar_fallback() -> None:
    ev = {
        "id": "e5", "parent_id": None, "agent_id": None,
        "timestamp": "2026-07-01T00:00:04+00:00", "type": "widget_frobbed",
        "payload": {"count": 3, "label": "hi"},
    }
    line = digest_event(ev)
    assert "widget_frobbed" in line
    assert "count=3" in line
    assert "label=hi" in line


def test_usage_patch_row_missing_envelope_renders_without_error() -> None:
    # llm_response_usage_patch rows are persisted raw: no id/parent_id/timestamp.
    ev = {
        "type": "llm_response_usage_patch", "agent_id": "step:x/y",
        "payload": {"usage": {"input_tokens": 7}},
    }
    line = digest_event(ev)
    assert "llm_response_usage_patch" in line
    assert "\n" not in line


def test_malformed_line_is_recognizable() -> None:
    line = digest_event({"_malformed": True, "raw": "{not valid json"})
    assert line.startswith("<malformed>")
    assert "{not valid json" in line


def test_llm_response_with_unparseable_raw_does_not_raise() -> None:
    ev = {
        "id": "e6", "parent_id": None, "agent_id": "step:x/y",
        "timestamp": "2026-07-01T00:00:05+00:00", "type": "llm_response",
        "payload": {
            "provider": "no_such_provider", "model": "m",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 1},
            "raw": "this is not a dict and no parser exists",
        },
    }
    line = digest_event(ev)  # must not raise
    assert "\n" not in line
    assert "end_turn" in line   # stop_reason still rendered from payload
    assert "tools=0" in line    # degraded fallback tool count


def test_user_message_digest_shows_role_and_text() -> None:
    ev = {
        "id": "e7", "parent_id": None, "agent_id": "step:x/y",
        "timestamp": "2026-07-01T00:00:06+00:00", "type": "user_message",
        "payload": {"role": "user", "content": [{"type": "text", "text": "hello there"}]},
    }
    line = digest_event(ev)
    assert "user" in line
    assert "hello there" in line
    assert "\n" not in line


def test_tool_call_digest_shows_name_and_input() -> None:
    ev = {
        "id": "e8", "parent_id": None, "agent_id": "sub:abc",
        "timestamp": "2026-07-01T00:00:07+00:00", "type": "tool_call",
        "payload": {"id": "t1", "name": "read_file", "input": {"path": "x.py"}},
    }
    line = digest_event(ev)
    assert "read_file" in line
    assert "x.py" in line
    assert "\n" not in line


def test_full_line_roundtrips_a_small_event() -> None:
    ev = {"id": "e", "parent_id": None, "agent_id": "step:x/y",
          "timestamp": "2026-07-01T00:00:00+00:00", "type": "tool_call",
          "payload": {"id": "t1", "name": "grep", "input": {"pattern": "z"}}}
    assert json.loads(full_line(ev)) == ev


def test_full_line_truncates_a_large_payload() -> None:
    ev = {"id": "e", "parent_id": None, "agent_id": "step:x/y",
          "timestamp": "2026-07-01T00:00:00+00:00", "type": "tool_result",
          "payload": {"id": "t1", "error": False, "content": "A" * 10000}}
    line = full_line(ev)
    assert line.endswith("…[truncated — narrow the query]")
    assert len(line) == _FULL_CAP + len(_TRUNC_MARK)


def test_full_line_redacts_step_context_secrets() -> None:
    ev = {"id": "e", "parent_id": None, "agent_id": "step:plan/plan-writer",
          "timestamp": "2026-07-01T00:00:00+00:00", "type": "step_context",
          "payload": {"provider": "anthropic", "model": "claude-x",
                      "system_prompt": "SECRET_SYSTEM_PROMPT_BODY",
                      "tools_schema": [{"name": "SECRET_TOOL_SCHEMA"}],
                      "params": {}, "max_tool_calls": 40}}
    line = full_line(ev)
    assert "SECRET_SYSTEM_PROMPT_BODY" not in line
    assert "SECRET_TOOL_SCHEMA" not in line
    assert "claude-x" in line


def test_warning_digest_shows_message() -> None:
    ev = {
        "id": "e9", "parent_id": None, "agent_id": None,
        "timestamp": "2026-07-01T00:00:08+00:00", "type": "warning",
        "payload": {
            "source": "artifact_harvest",
            "message": "artifact ref refs/imp/artifacts/x unreadable",
            "detail": {},
        },
    }
    line = digest_event(ev)
    assert "warning: artifact ref refs/imp/artifacts/x unreadable" in line
    assert "\n" not in line


def test_warning_digest_clips_long_message() -> None:
    long_msg = "x" * 500
    ev = {
        "id": "e10", "parent_id": None, "agent_id": None,
        "timestamp": "2026-07-01T00:00:09+00:00", "type": "warning",
        "payload": {"message": long_msg},
    }
    line = digest_event(ev)
    assert line.startswith("2026-07-01T00:00:09+00:00 warning")
    assert "warning: " in line
    assert "\n" not in line
    assert len(line) < len(long_msg)


def test_warning_digest_missing_message_key() -> None:
    ev = {
        "id": "e11", "parent_id": None, "agent_id": None,
        "timestamp": "2026-07-01T00:00:10+00:00", "type": "warning",
        "payload": {"source": "some_source"},
    }
    line = digest_event(ev)
    assert "warning: " in line
    assert "\n" not in line
