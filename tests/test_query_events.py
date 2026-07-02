# tests/test_query_events.py
from __future__ import annotations

import json
from pathlib import Path

from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine.state import JsonlEventLog


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(project_root=tmp_path, events_path=tmp_path / "events.jsonl")


def _call(tmp_path: Path, **args) -> str:
    return get_registry()["query_events"].invoke(_ctx(tmp_path), args)


def _lines(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln]


# Synthetic events.jsonl: named step:/sub: agent_ids, a step_context carrying
# system_prompt + tools_schema, an llm_response_usage_patch row lacking
# id/parent_id/timestamp, an unknown type, and a malformed line.
_EVENTS: list[dict] = [
    {"id": "1", "parent_id": None, "agent_id": "step:plan/plan-writer",
     "timestamp": "2026-07-01T00:00:00+00:00", "type": "step_context",
     "payload": {"provider": "anthropic", "model": "claude-x",
                 "system_prompt": "SECRET_SYSTEM_PROMPT_BODY",
                 "tools_schema": [{"name": "SECRET_TOOL_SCHEMA"}],
                 "params": {}, "max_tool_calls": 40}},
    {"id": "2", "parent_id": "1", "agent_id": "step:plan/plan-writer",
     "timestamp": "2026-07-01T00:00:01+00:00", "type": "user_message",
     "payload": {"role": "user", "content": [{"type": "text", "text": "hello there"}]}},
    {"id": "3", "parent_id": "2", "agent_id": "step:plan/plan-writer",
     "timestamp": "2026-07-01T00:00:02+00:00", "type": "llm_response",
     "payload": {"provider": "anthropic", "model": "claude-x",
                 "stop_reason": "end_turn",
                 "usage": {"input_tokens": 10, "output_tokens": 5},
                 "raw": {"content": [{"type": "text", "text": "sure"}]}}},
    {"id": "4", "parent_id": None, "agent_id": "sub:abc123",
     "timestamp": "2026-07-01T00:00:03+00:00", "type": "tool_call",
     "payload": {"id": "t1", "name": "read_file", "input": {"path": "x.py"}}},
    {"id": "5", "parent_id": None, "agent_id": "sub:abc123",
     "timestamp": "2026-07-01T00:00:04+00:00", "type": "widget_frobbed",
     "payload": {"count": 3}},
    {"type": "llm_response_usage_patch", "agent_id": "step:plan/plan-writer",
     "payload": {"usage": {"input_tokens": 7}}},
]


def _write_log(tmp_path: Path) -> None:
    lines = [json.dumps(e) for e in _EVENTS]
    lines.append("{not valid json")  # malformed line
    (tmp_path / "events.jsonl").write_text("\n".join(lines) + "\n")


def test_slim_default_is_one_line_per_event_not_raw_json(tmp_path: Path) -> None:
    _write_log(tmp_path)
    out = _call(tmp_path)
    lines = _lines(out)
    assert len(lines) == len(_EVENTS) + 1  # +1 malformed line
    # Slim, not raw JSON: the step_context line exposes model but neither the
    # system_prompt nor the tools_schema body.
    ctx_line = next(ln for ln in lines if "step_context" in ln)
    assert "claude-x" in ctx_line
    assert "SECRET_SYSTEM_PROMPT_BODY" not in out
    assert "SECRET_TOOL_SCHEMA" not in out


def test_type_filter_single(tmp_path: Path) -> None:
    _write_log(tmp_path)
    lines = _lines(_call(tmp_path, type="llm_response"))
    assert len(lines) == 1
    assert "llm_response" in lines[0]


def test_type_filter_comma_separated_multi(tmp_path: Path) -> None:
    _write_log(tmp_path)
    lines = _lines(_call(tmp_path, type="user_message,tool_call"))
    assert len(lines) == 2
    assert any("user_message" in ln for ln in lines)
    assert any("tool_call" in ln for ln in lines)


def test_agent_id_substring_matches_named_step(tmp_path: Path) -> None:
    _write_log(tmp_path)
    lines = _lines(_call(tmp_path, agent_id="plan/"))
    # Matches every step:plan/plan-writer event (incl. the usage_patch row);
    # excludes the sub:abc123 ones.
    assert len(lines) == 4
    assert all("plan/plan-writer" in ln for ln in lines)
    assert not any("sub:abc123" in ln for ln in lines)


def test_agent_id_substring_matches_opaque_sub_id(tmp_path: Path) -> None:
    _write_log(tmp_path)
    lines = _lines(_call(tmp_path, agent_id="sub:"))
    assert len(lines) == 2
    assert all("sub:abc123" in ln for ln in lines)


def test_limit_default_returns_last_100(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    for i in range(150):
        log.append(agent_id=None, type="tick", payload={"i": i})
    lines = _lines(_call(tmp_path))
    assert len(lines) == 100
    assert "i=50" in lines[0]     # oldest kept
    assert "i=149" in lines[-1]   # newest


def test_explicit_limit_tails(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    for i in range(10):
        log.append(agent_id=None, type="tick", payload={"i": i})
    lines = _lines(_call(tmp_path, limit=3))
    assert len(lines) == 3
    assert "i=7" in lines[0]
    assert "i=9" in lines[-1]


def test_quoted_string_limit_is_coerced(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    for i in range(10):
        log.append(agent_id=None, type="tick", payload={"i": i})
    lines = _lines(_call(tmp_path, limit="2"))
    assert len(lines) == 2


def test_invalid_limit_returns_single_line_json_error(tmp_path: Path) -> None:
    out = _call(tmp_path, limit="abc")
    lines = _lines(out)
    assert len(lines) == 1
    assert "error" in json.loads(lines[0])


def test_empty_file_returns_empty(tmp_path: Path) -> None:
    assert _call(tmp_path) == ""


def test_malformed_line_rendered_recognizably(tmp_path: Path) -> None:
    _write_log(tmp_path)
    out = _call(tmp_path)
    assert any(ln.startswith("<malformed>") for ln in _lines(out))


def test_no_filter_path_uses_tail(tmp_path: Path, mocker) -> None:
    _write_log(tmp_path)
    spy = mocker.spy(JsonlEventLog, "tail")
    _call(tmp_path)
    assert spy.call_count == 1


def test_filtered_path_does_not_use_tail(tmp_path: Path, mocker) -> None:
    _write_log(tmp_path)
    spy = mocker.spy(JsonlEventLog, "tail")
    _call(tmp_path, type="llm_response")
    assert spy.call_count == 0


def test_non_positive_limit_clamps_to_default(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    for i in range(150):
        log.append(agent_id=None, type="tick", payload={"i": i})
    # Both 0 and a negative limit clamp to the bounded default (100), NOT "all".
    assert len(_lines(_call(tmp_path, limit=0))) == 100
    assert len(_lines(_call(tmp_path, limit=-5))) == 100


def test_agent_id_only_path_does_not_use_tail(tmp_path: Path, mocker) -> None:
    _write_log(tmp_path)
    spy = mocker.spy(JsonlEventLog, "tail")
    _call(tmp_path, agent_id="sub:")
    assert spy.call_count == 0
