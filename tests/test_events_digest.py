# tests/test_events_digest.py
from __future__ import annotations

from imp.engine.events_digest import digest_event


def test_digest_event_is_single_line() -> None:
    ev = {
        "id": "e1", "parent_id": None, "agent_id": "step:plan/plan-writer",
        "timestamp": "2026-07-01T00:00:00+00:00", "type": "awaiting_user_input",
        "payload": {"steer_hint": "line one\nline two\nline three"},
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


def test_llm_response_digest_shows_stop_tools_in_out_and_text() -> None:
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
    assert "stop=tool_use" in line
    assert "tools=1" in line
    assert "in=111" in line
    assert "out=22" in line
    assert "here is my analysis" in line
    assert "usage=" not in line          # the raw usage dict is gone


def test_tool_result_digest_shows_error_bytes_and_no_id() -> None:
    ev = {
        "id": "e4", "parent_id": None, "agent_id": "step:x/y",
        "timestamp": "2026-07-01T00:00:03+00:00", "type": "tool_result",
        "payload": {"id": "toolu_abc", "content": "ABCDEFGHIJ", "error": True},
    }
    line = digest_event(ev)
    assert "error=True" in line
    assert "bytes=10" in line          # byte size of "ABCDEFGHIJ"
    assert "id=" not in line           # opaque tool-use id dropped
    assert "toolu_abc" not in line


def test_tool_result_digest_unwraps_text_content_block() -> None:
    ev = {
        "id": "e4b", "parent_id": None, "agent_id": "step:x/y",
        "timestamp": "2026-07-01T00:00:03+00:00", "type": "tool_result",
        "payload": {"id": "toolu_x", "error": False,
                    "content": [{"type": "text", "text": "unwrapped body"}]},
    }
    line = digest_event(ev)
    assert "unwrapped body" in line
    assert '"type"' not in line         # the raw block wrapper is gone
    assert "bytes=14" in line           # byte size of "unwrapped body"


def test_tool_result_digest_json_dumps_non_text_content() -> None:
    ev = {
        "id": "e4c", "parent_id": None, "agent_id": "step:x/y",
        "timestamp": "2026-07-01T00:00:03+00:00", "type": "tool_result",
        "payload": {"error": False,
                    "content": [{"type": "image", "source": "data:xyz"}]},
    }
    line = digest_event(ev)
    assert "image" in line              # non-text content falls back to json.dumps
    assert "data:xyz" in line


def test_tool_result_digest_handles_missing_content() -> None:
    ev = {
        "id": "e4d", "parent_id": None, "agent_id": "step:x/y",
        "timestamp": "2026-07-01T00:00:03+00:00", "type": "tool_result",
        "payload": {"error": False},
    }
    line = digest_event(ev)          # must not raise
    assert "error=False" in line
    assert "\n" not in line


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


def test_summarize_exposes_type_aware_tail() -> None:
    from imp.engine.events_digest import summarize
    tail = summarize({"type": "tool_call",
                      "payload": {"name": "grep", "input": {"pattern": "x"}}})
    assert "grep" in tail
    # the type-aware tool_call handler json-serializes the input dict; the
    # generic fallback would render it as `input=<dict>` instead.
    assert '"pattern": "x"' in tail


def test_step_started_digest_positive_selects_five_fields() -> None:
    ev = {
        "id": "e12", "parent_id": None, "agent_id": None,
        "timestamp": "2026-07-01T00:00:11+00:00", "type": "step_started",
        "payload": {
            "step": "plan-writer", "phase": "plan", "kind": "agent",
            "model": "claude-x", "provider": "anthropic",
            "show_label": True, "show_output": False, "label": "Planning",
            "tools": ["read_file", "grep"], "resumed": False, "command": "noop",
        },
    }
    line = digest_event(ev)
    assert "step=plan-writer" in line
    assert "phase=plan" in line
    assert "kind=agent" in line
    assert "model=claude-x" in line
    assert "provider=anthropic" in line
    # dropped-by-construction render/lifecycle keys never leak
    assert "show_label" not in line
    assert "show_output" not in line
    assert "Planning" not in line
    assert "resumed" not in line
    assert "noop" not in line
    assert "\n" not in line


def test_step_started_script_step_renders_none_model_provider() -> None:
    ev = {
        "id": "e13", "parent_id": None, "agent_id": None,
        "timestamp": "2026-07-01T00:00:12+00:00", "type": "step_started",
        "payload": {"step": "verify", "phase": "impl", "kind": "script",
                    "command": "pytest -q", "show_label": True},
    }
    line = digest_event(ev)
    assert "step=verify" in line
    assert "kind=script" in line
    assert "model=None" in line          # absent on script/gate emits
    assert "provider=None" in line
    assert "pytest" not in line          # command dropped
    assert "\n" not in line


def test_query_result_digest_positive_selects_and_humanizes() -> None:
    ev = {
        "id": "e14", "parent_id": None, "agent_id": "step:x/y",
        "timestamp": "2026-07-01T00:00:13+00:00", "type": "query_result",
        "payload": {
            "total_cost_usd": 0.12345, "duration_ms": 4500,
            "duration_api_ms": 4200, "num_turns": 7,
            "stop_reason": "end_turn", "subtype": "success", "is_error": False,
            "usage": {"input_tokens": 5}, "model_usage": {"claude-x": {}},
            "permission_denials": [{"tool": "bash"}],
        },
    }
    line = digest_event(ev)
    assert "cost=$0.12" in line          # rounded from 0.12345
    assert "dur=4.5s" in line            # 4500ms humanized
    assert "turns=7" in line
    assert "stop=end_turn" in line
    assert "subtype=success" in line
    # noisy dict/list placeholders and bookkeeping keys dropped by construction
    assert "usage=" not in line
    assert "model_usage" not in line
    assert "permission_denials" not in line
    assert "duration_api_ms" not in line
    assert "\n" not in line


def test_query_result_digest_renders_error_subtype() -> None:
    ev = {
        "id": "e15", "parent_id": None, "agent_id": "step:x/y",
        "timestamp": "2026-07-01T00:00:14+00:00", "type": "query_result",
        "payload": {"total_cost_usd": 0.0, "duration_ms": 100, "num_turns": 1,
                    "stop_reason": None, "subtype": "error_during_execution",
                    "is_error": True, "usage": {}, "model_usage": {}},
    }
    line = digest_event(ev)
    assert "subtype=error_during_execution" in line
    assert "cost=$0.00" in line


def test_query_result_digest_guards_none_cost_and_duration() -> None:
    ev = {
        "id": "e16", "parent_id": None, "agent_id": "step:x/y",
        "timestamp": "2026-07-01T00:00:15+00:00", "type": "query_result",
        "payload": {"total_cost_usd": None, "duration_ms": None,
                    "num_turns": 0, "stop_reason": None, "subtype": None},
    }
    line = digest_event(ev)              # must not raise on None numerics
    assert "cost=$None" in line
    assert "dur=Nones" in line
    assert "\n" not in line
