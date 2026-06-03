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
        fs_permissions=(), events_path=tmp_path / "events.jsonl",
    )


def test_query_events_returns_recent(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    log.append(agent_id=None, type="phase_started", payload={"phase": "spec"})
    log.append(agent_id=None, type="step_started", payload={"step": "spec-writer"})
    out = get_registry()["query_events"].invoke(_ctx(tmp_path), {"limit": 1})
    events = json.loads(out)
    assert len(events) == 1
    assert events[0]["type"] == "step_started"
