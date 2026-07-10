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


def test_full_true_returns_raw_json_objects(tmp_path: Path) -> None:
    _write_log(tmp_path)
    lines = _lines(_call(tmp_path, full=True))
    # Every non-truncated line parses back to a JSON object.
    parsed = [json.loads(ln) for ln in lines if "…[truncated" not in ln]
    assert any(obj.get("type") == "llm_response" for obj in parsed)


def test_full_true_redacts_step_context(tmp_path: Path) -> None:
    _write_log(tmp_path)
    out = _call(tmp_path, full=True)
    assert "SECRET_SYSTEM_PROMPT_BODY" not in out
    assert "SECRET_TOOL_SCHEMA" not in out


def test_full_no_filter_matches_manual_tail(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    for i in range(10):
        log.append(agent_id=None, type="tick", payload={"i": i})
    from imp.engine.events_digest import full_line
    expected = [full_line(e) for e in list(log.read())[-3:]]
    assert _lines(_call(tmp_path, limit=3, full=True)) == expected


def test_description_frames_provenance_documents_new_params() -> None:
    desc = get_registry()["query_events"].description
    low = desc.lower()
    # Provenance framing, not the old state/format framing.
    assert "provenance" in low
    assert "what was said" in low
    assert "greppable" not in low
    assert "one json event per line" not in low
    # Documents the two params added in Part 1.
    assert "full=True" in desc
    assert "agent_id" in desc
    assert "substring" in low
    assert "comma-separated" in low
    assert "limit" in low
    assert "default 100" in low
    # Generic current-state nudge, and no workflow artifact filenames.
    assert "current state" in low
    assert ".md" not in desc


def _control_events_path(control: Path, cid: str) -> Path:
    """Build <control>/.imp/changes/<cid>/ and return its events.jsonl path."""
    d = control / ".imp" / "changes" / cid
    d.mkdir(parents=True, exist_ok=True)
    return d / "events.jsonl"


def _ctx_at(events_path: Path) -> ToolContext:
    return ToolContext(project_root=events_path.parent, events_path=events_path)


def _call_at(events_path: Path, **args) -> str:
    return get_registry()["query_events"].invoke(_ctx_at(events_path), args)


def _make_live_change(control: Path, cid: str, events: list[dict]) -> None:
    """A live change home with a minimal change.json (layout_version pinned to
    the current LAYOUT_VERSION so Change.load skips migrate) and events."""
    from imp.engine.change import LAYOUT_VERSION

    d = control / ".imp" / "changes" / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "change.json").write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "project_root": str(control), "layout_version": LAYOUT_VERSION}))
    log = JsonlEventLog(d / "events.jsonl")
    for e in events:
        log.append(agent_id=e["agent_id"], type=e["type"], payload=e["payload"])


def _git_init(root: Path) -> None:
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)


def _archive_quest(control: Path, cid: str, events_bytes: bytes) -> None:
    """Archive a change (NO live .imp/changes/<cid> home) so resolution falls
    through Change.load (LoadError) to the archive path."""
    from imp.engine import archive

    events = control / f"{cid}.jsonl"
    events.write_bytes(events_bytes)
    quest_json = control / f"{cid}-change.json"
    quest_json.write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main"}))
    state_json = control / f"{cid}-state.json"
    state_json.write_text(json.dumps({"id": cid, "status": "completed", "step": None}))
    archive.archive_quest(
        control, cid, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)


def test_quest_id_live_returns_that_changes_digest(tmp_path: Path) -> None:
    cur = _control_events_path(tmp_path, "curr000")
    _make_live_change(tmp_path, "othr111", [
        {"agent_id": "step:plan/plan-writer", "type": "user_message",
         "payload": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        {"agent_id": "sub:abc123", "type": "tool_call",
         "payload": {"id": "t1", "name": "read_file", "input": {}}},
    ])
    lines = [ln for ln in _call_at(cur, quest_id="othr111").splitlines() if ln]
    assert len(lines) == 2
    assert any("user_message" in ln for ln in lines)
    assert any("tool_call" in ln for ln in lines)


def test_quest_id_archived_resolves(tmp_path: Path) -> None:
    _git_init(tmp_path)
    cur = _control_events_path(tmp_path, "curr000")
    events_bytes = (
        b'{"id": "a-e0", "parent_id": null, "agent_id": "step:review/final-review",'
        b' "timestamp": "2026-06-01T00:00:00+00:00", "type": "step_started",'
        b' "payload": {"step": "final-review"}}\n'
    )
    _archive_quest(tmp_path, "arch222", events_bytes)
    lines = [ln for ln in _call_at(cur, quest_id="arch222").splitlines() if ln]
    assert len(lines) == 1
    assert "step_started" in lines[0]
    assert "review/final-review" in lines[0]


def test_quest_id_unresolvable_returns_error_naming_id(tmp_path: Path) -> None:
    cur = _control_events_path(tmp_path, "curr000")
    out = _call_at(cur, quest_id="ghost99")
    lines = [ln for ln in out.splitlines() if ln]
    assert len(lines) == 1
    err = json.loads(lines[0])
    assert "error" in err
    assert "ghost99" in err["error"]


def test_quest_id_naming_current_reads_current_events(tmp_path: Path) -> None:
    # The current change home has NO change.json; without the short-circuit the
    # resolver would hit Change.load and blow up, so this pins the short-circuit.
    cur = _control_events_path(tmp_path, "curr000")
    log = JsonlEventLog(cur)
    log.append(agent_id="step:plan/plan-writer", type="user_message",
               payload={"role": "user", "content": [{"type": "text", "text": "x"}]})
    omitted = _call_at(cur)
    named = _call_at(cur, quest_id="curr000")
    assert named == omitted
    assert omitted != ""


def test_quest_id_applies_type_and_agent_filters(tmp_path: Path) -> None:
    cur = _control_events_path(tmp_path, "curr000")
    _make_live_change(tmp_path, "othr111", [
        {"agent_id": "step:plan/plan-writer", "type": "user_message",
         "payload": {"role": "user", "content": [{"type": "text", "text": "a"}]}},
        {"agent_id": "step:plan/plan-writer", "type": "llm_response",
         "payload": {"provider": "p", "model": "m", "stop_reason": "end_turn",
                     "usage": {}, "raw": {"content": []}}},
        {"agent_id": "sub:abc123", "type": "tool_call",
         "payload": {"id": "t1", "name": "read_file", "input": {}}},
    ])
    type_lines = [ln for ln in
                  _call_at(cur, quest_id="othr111", type="llm_response").splitlines() if ln]
    assert len(type_lines) == 1
    assert "llm_response" in type_lines[0]

    agent_lines = [ln for ln in
                   _call_at(cur, quest_id="othr111", agent_id="sub:").splitlines() if ln]
    assert len(agent_lines) == 1
    assert "sub:abc123" in agent_lines[0]


def test_quest_id_applies_limit(tmp_path: Path) -> None:
    cur = _control_events_path(tmp_path, "curr000")
    _make_live_change(tmp_path, "othr111", [
        {"agent_id": "step:plan/plan-writer", "type": "tick", "payload": {"i": i}}
        for i in range(10)
    ])
    lines = [ln for ln in
             _call_at(cur, quest_id="othr111", limit=3).splitlines() if ln]
    assert len(lines) == 3
    assert "i=7" in lines[0]
    assert "i=8" in lines[1]
    assert "i=9" in lines[2]
    assert "i=0" not in lines[0]


def test_description_documents_quest_id_and_current_change() -> None:
    desc = get_registry()["query_events"].description
    assert "quest_id" in desc
    assert "current change" in desc.lower()
