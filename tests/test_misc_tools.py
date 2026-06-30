# tests/test_misc_tools.py
from __future__ import annotations

import json
from pathlib import Path

from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine.state import JsonlEventLog


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        project_root=tmp_path,
events_path=tmp_path / "events.jsonl",
    )


def _lines(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line]


def test_query_events_returns_recent_as_jsonl(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    log.append(agent_id=None, type="phase_started", payload={"phase": "spec"})
    log.append(agent_id=None, type="step_started", payload={"step": "spec-writer"})
    out = get_registry()["query_events"].invoke(_ctx(tmp_path), {"limit": 1})
    events = _lines(out)
    assert len(events) == 1
    assert events[0]["type"] == "step_started"
    # JSONL shape: the single event is its own line, NOT a JSON array.
    assert not out.lstrip().startswith("[")


def test_query_events_bare_call_is_jsonl_capped_at_last_100(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    for i in range(150):
        log.append(agent_id=None, type="tick", payload={"i": i})
    out = get_registry()["query_events"].invoke(_ctx(tmp_path), {})
    events = _lines(out)
    # Bounded: a bare call returns only the last 100 events, one per line.
    assert len(events) == 100
    assert events[0]["payload"]["i"] == 50
    assert events[-1]["payload"]["i"] == 149


def test_query_events_type_and_explicit_limit_still_filter(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    log.append(agent_id=None, type="phase_started", payload={"phase": "spec"})
    log.append(agent_id=None, type="step_started", payload={"step": "a"})
    log.append(agent_id=None, type="step_started", payload={"step": "b"})
    out = get_registry()["query_events"].invoke(
        _ctx(tmp_path), {"type": "step_started", "limit": 1})
    events = _lines(out)
    assert len(events) == 1
    assert events[0]["payload"]["step"] == "b"


def test_query_events_description_states_jsonl_and_default_cap() -> None:
    desc = get_registry()["query_events"].description
    assert "JSONL" in desc
    assert "100" in desc


def test_query_events_invalid_limit_returns_single_line_json_error(tmp_path: Path) -> None:
    out = get_registry()["query_events"].invoke(_ctx(tmp_path), {"limit": "abc"})
    # Error path: a single json.dumps({"error": ...}) line — valid JSONL.
    lines = [l for l in out.splitlines() if l]
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert "error" in obj
