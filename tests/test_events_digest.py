# tests/test_events_digest.py
from __future__ import annotations

from typing import Any

import pytest

from imp.engine.events_digest import digest_event, summarize


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


def test_agent_message_digest_shows_stop_tools_in_out_and_text() -> None:
    ev = {
        "id": "e3", "parent_id": None, "agent_id": "step:x/y",
        "timestamp": "2026-07-01T00:00:02+00:00", "type": "agent_message",
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
    # agent_message_usage_patch rows are persisted raw: no id/parent_id/timestamp.
    ev = {
        "type": "agent_message_usage_patch", "agent_id": "step:x/y",
        "payload": {"usage": {"input_tokens": 7}},
    }
    line = digest_event(ev)
    assert "agent_message_usage_patch" in line
    assert "\n" not in line


def test_malformed_line_is_recognizable() -> None:
    line = digest_event({"_malformed": True, "raw": "{not valid json"})
    assert line.startswith("<malformed>")
    assert "{not valid json" in line


def test_agent_message_with_unparseable_raw_does_not_raise() -> None:
    ev = {
        "id": "e6", "parent_id": None, "agent_id": "step:x/y",
        "timestamp": "2026-07-01T00:00:05+00:00", "type": "agent_message",
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
        "payload": {
            "role": "user", "subtype": "intent",
            "content": [{"type": "text", "text": "hello there"}],
        },
    }
    line = digest_event(ev)
    assert "user" in line
    assert "hello there" in line
    assert "intent" in line
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


def test_warning_digest_preserves_full_message() -> None:
    long_msg = "x" * 500
    ev = {
        "id": "e10", "parent_id": None, "agent_id": None,
        "timestamp": "2026-07-01T00:00:09+00:00", "type": "warning",
        "payload": {"message": long_msg},
    }
    line = digest_event(ev)
    assert line.startswith("2026-07-01T00:00:09+00:00 warning")
    assert long_msg in line          # per-field cap removed: full message survives
    assert "\n" not in line          # whitespace-collapse keeps one logical line


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
    tail = summarize({"type": "tool_call",
                      "payload": {"name": "grep", "input": {"pattern": "x"}}})
    assert "grep" in tail
    # the type-aware tool_call handler leads with the tool name, then the input as JSON.
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


def test_engine_system_prompt_text_is_reachable():
    ev = {"id": "e1", "type": "engine_message", "agent_id": "step:build/plan",
          "payload": {"role": "user", "subtype": "system_prompt",
                      "content": [{"type": "text",
                                   "text": "You are a coder in imp."}]}}
    line = digest_event(ev)
    assert "system_prompt" in line
    assert "You are a coder in imp." in line


def test_agent_message_digest_reports_tools_and_stop():
    ev = {"id": "e1", "type": "agent_message", "agent_id": "step:build/plan",
          "payload": {"provider": "anthropic", "model": "m",
                      "stop_reason": "end_turn",
                      "usage": {"input_tokens": 3, "output_tokens": 5},
                      "raw": {"content": [{"type": "text", "text": "hi"}]}}}
    line = digest_event(ev)
    assert "stop=end_turn" in line
    assert "hi" in line


def test_agent_message_ctx_resolves_window_from_the_catalog() -> None:
    # anthropic/openai report context_tokens alone: the window comes from the
    # model catalog, and the percentage is against that window's handoff limit
    # (the 200000 ceiling, below 80% of the 1M window), not the window.
    ev = {
        "id": "c1", "type": "agent_message", "agent_id": "step:build/coder",
        "payload": {
            "provider": "anthropic", "model": "claude-opus-5",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "context_tokens": 159_842,
            "raw": {"content": [{"type": "text", "text": "done"}]},
        },
    }
    assert "ctx=159842/200000 (79.9%)" in digest_event(ev)


def test_agent_message_ctx_prefers_the_reported_window() -> None:
    # claude reports its own window; it wins over the catalog's (200000 for
    # sonnet, limit 160000), giving the 200000 ceiling here. Occupancy past the
    # limit reports past 100%, and the legacy percentage is ignored — 203000/200000
    # is 101.5%, not the 79.9% the payload claims.
    ev = {
        "id": "c2", "type": "agent_message", "agent_id": "step:build/coder",
        "payload": {
            "provider": "claude", "model": "claude-sonnet-4-6",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "context_tokens": 203_000, "context_window": 1_000_000,
            "context_pct": 79.85263,
            "raw": {"content": [{"type": "text", "text": "done"}]},
        },
    }
    line = digest_event(ev)
    assert "ctx=203000/200000 (101.5%)" in line
    assert "160000" not in line
    assert "79.9" not in line


def test_agent_message_ctx_omitted_when_tokens_are_absent() -> None:
    # A legacy payload's percentage is inert: without tokens there is nothing to
    # report, and the old percent-only line is gone.
    ev = {
        "id": "c3", "type": "agent_message", "agent_id": "step:build/coder",
        "payload": {
            "provider": "claude", "model": "claude-opus-5",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "context_pct": 79.85263,
            "raw": {"content": [{"type": "text", "text": "done"}]},
        },
    }
    line = digest_event(ev)
    assert "ctx=" not in line
    assert "79.9" not in line


def test_legacy_context_pct_key_does_not_change_the_digest() -> None:
    # An event log written before context_pct was dropped digests identically to
    # one written after: the key is inert, not an error.
    def _ev(**extra):
        return {
            "id": "c3c", "type": "agent_message", "agent_id": "step:build/coder",
            "payload": {
                "provider": "anthropic", "model": "claude-opus-5",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "context_tokens": 159_842,
                "raw": {"content": [{"type": "text", "text": "done"}]},
                **extra,
            },
        }
    assert digest_event(_ev(context_pct=79.85263)) == digest_event(_ev())


def test_agent_message_ctx_omitted_when_the_window_cannot_be_resolved() -> None:
    # Unknown provider/model, no reported window, no percentage: never guess.
    ev = {
        "id": "c4", "type": "agent_message", "agent_id": "step:build/coder",
        "payload": {
            "provider": "no_such_provider", "model": "m",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 1},
            "context_tokens": 5000,
            "raw": {"content": [{"type": "text", "text": "done"}]},
        },
    }
    line = digest_event(ev)
    assert "ctx=" not in line
    assert "5000" not in line


def test_agent_message_without_occupancy_fields_renders_no_ctx() -> None:
    ev = {
        "id": "c5", "type": "agent_message", "agent_id": "step:build/coder",
        "payload": {
            "provider": "anthropic", "model": "claude-opus-5",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 1},
            "raw": {"content": [{"type": "text", "text": "done"}]},
        },
    }
    assert "ctx=" not in digest_event(ev)


def test_agent_message_ctx_ignores_non_numeric_occupancy_fields() -> None:
    ev = {
        "id": "c6", "type": "agent_message", "agent_id": "step:build/coder",
        "payload": {
            "provider": "anthropic", "model": "claude-opus-5",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 1},
            "context_tokens": "lots", "context_pct": "high",
            "raw": {"content": [{"type": "text", "text": "done"}]},
        },
    }
    line = digest_event(ev)
    assert "ctx=" not in line
    assert "stop=end_turn" in line


def _tier(provider: str, model: str, differ_from: str | None = None,
          differ_tier: int | None = None, **extra: Any) -> dict[str, Any]:
    return {"provider": provider, "model": model,
            "differ_from": differ_from, "differ_tier": differ_tier, **extra}


def _tiers_ev(payload: dict[str, Any]) -> dict[str, Any]:
    return {"id": "t1", "parent_id": None, "agent_id": None,
            "timestamp": "2026-07-01T00:00:20+00:00", "type": "tiers_resolved",
            "payload": payload}


_THREE_TIERS = {
    "low": _tier("claude", "claude-haiku-5-5"),
    "medium": _tier("claude", "claude-sonnet-5-5"),
    "high": _tier("claude", "claude-opus-5-5"),
}

_TIERS_WITH_STEP_OVERRIDE = {
    "tiers": {
        "low": _tier("claude", "claude-haiku-4-5-20251001"),
        "medium": _tier("claude", "claude-sonnet-4-6"),
        "high": _tier("claude", "claude-opus-4-7"),
    },
    "steps": {
        "code-reviewer": _tier("claude", "claude-sonnet-4-6",
                               differ_from="task-lead", differ_tier=2),
    },
}


def test_tiers_resolved_renders_tiers_and_empty_steps() -> None:
    assert summarize(_tiers_ev({"tiers": _THREE_TIERS, "steps": {}})) == (
        "tiers: low=claude/claude-haiku-5-5 medium=claude/claude-sonnet-5-5 "
        "high=claude/claude-opus-5-5 steps: none"
    )


def test_tiers_resolved_step_override_carries_divergence_suffix() -> None:
    assert summarize(_tiers_ev(_TIERS_WITH_STEP_OVERRIDE)) == (
        "tiers: low=claude/claude-haiku-4-5-20251001 medium=claude/claude-sonnet-4-6 "
        "high=claude/claude-opus-4-7 steps: code-reviewer=claude/claude-sonnet-4-6 "
        "(differs from task-lead: tier 2)"
    )


def test_tiers_resolved_tier_entry_carries_divergence_suffix() -> None:
    tiers = {
        "medium": _tier("anthropic", "sonnet"),
        "high": _tier("openai", "gpt-5.5", differ_from="medium", differ_tier=1),
    }
    assert summarize(_tiers_ev({"tiers": tiers, "steps": {}})) == (
        "tiers: medium=anthropic/sonnet "
        "high=openai/gpt-5.5 (differs from medium: tier 1) steps: none"
    )


def test_tiers_resolved_ignores_legacy_extra_keys() -> None:
    legacy = {name: {**entry, "warning": "old codex selection warning"}
              for name, entry in _THREE_TIERS.items()}
    assert (summarize(_tiers_ev({"tiers": legacy, "steps": {}}))
            == summarize(_tiers_ev({"tiers": _THREE_TIERS, "steps": {}})))


def test_tiers_resolved_empty_payload_renders_none_sections() -> None:
    assert summarize(_tiers_ev({})) == "tiers: none steps: none"


def test_tiers_resolved_non_dict_section_renders_none() -> None:
    assert summarize(_tiers_ev({"tiers": "garbage", "steps": ["x"]})) == (
        "tiers: none steps: none"
    )


def test_tiers_resolved_non_dict_entry_renders_as_json() -> None:
    payload = {
        "tiers": {"low": "oops", "high": _tier("claude", "claude-opus-5-5")},
        "steps": {"coder": ["a", "b"]},
    }
    assert summarize(_tiers_ev(payload)) == (
        'tiers: low="oops" high=claude/claude-opus-5-5 steps: coder=["a", "b"]'
    )


def test_tiers_resolved_digest_line_keeps_envelope() -> None:
    line = digest_event(_tiers_ev({"tiers": _THREE_TIERS, "steps": {}}))
    assert line.startswith("2026-07-01T00:00:20+00:00 tiers_resolved [-] tiers: low=")
    assert "<dict>" not in line


_STEP_COMPLETED = {
    "id": "g1", "parent_id": None, "agent_id": None,
    "timestamp": "2026-07-01T00:00:21+00:00", "type": "step_completed",
    "payload": {
        "step": "threshold", "kind": "agent",
        "action": {"verb": "goto", "target": "scope-code-check",
                   "resume": False, "rework": False},
        "var_mutations": {"fast_path": False},
        "label": None,
    },
}

_QUOTA_READING = {
    "id": "g2", "parent_id": None, "agent_id": None,
    "timestamp": "2026-07-01T00:00:22+00:00", "type": "quota_reading",
    "payload": {"windows": [{"name": "5h", "pct": 41.5}, {"name": "7d", "pct": 12}],
                "raw": {"five_hour": 41.5}},
}

_ERROR_WITH_NEWLINE = {
    "id": "g3", "parent_id": None, "agent_id": None,
    "timestamp": "2026-07-01T00:00:23+00:00", "type": "error",
    "payload": {"message": "boom", "detail": {"trace": "line one\nline two"}},
}


def test_generic_fallback_renders_nested_values_as_inline_json() -> None:
    assert summarize(_STEP_COMPLETED) == (
        'step=threshold kind=agent action={"verb": "goto", "target": "scope-code-check", '
        '"resume": false, "rework": false} var_mutations={"fast_path": false} label=None'
    )


def test_generic_fallback_renders_list_as_json_array() -> None:
    line = summarize(_QUOTA_READING)
    assert 'windows=[{"name": "5h", "pct": 41.5}, {"name": "7d", "pct": 12}]' in line
    assert 'raw={"five_hour": 41.5}' in line


def test_generic_fallback_escapes_embedded_newline() -> None:
    line = digest_event(_ERROR_WITH_NEWLINE)
    assert "\n" not in line
    assert 'detail={"trace": "line one\\nline two"}' in line


@pytest.mark.parametrize("event", [
    _tiers_ev({"tiers": _THREE_TIERS, "steps": {}}),
    _tiers_ev(_TIERS_WITH_STEP_OVERRIDE),
    _tiers_ev({}),
    _tiers_ev({"tiers": "garbage", "steps": ["x"]}),
    _tiers_ev({"tiers": {"low": "line\nbreak"}, "steps": {"a\nb": {"provider": "p"}}}),
    _STEP_COMPLETED,
    _QUOTA_READING,
    _ERROR_WITH_NEWLINE,
])
def test_nested_payload_digest_has_no_placeholder_and_one_line(event: dict[str, Any]) -> None:
    for line in (summarize(event), digest_event(event)):
        assert "<dict>" not in line
        assert "<list>" not in line
        assert "\n" not in line
