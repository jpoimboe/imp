# tests/test_misc_tools.py
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import imp.agents.tools.git_diff
import imp.agents.tools.query_events
import imp.agents.tools.web_fetch  # noqa: F401
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine.state import JsonlEventLog


def _ctx(tmp_path: Path) -> ToolContext:
    artifacts = tmp_path / "_artifacts"
    artifacts.mkdir()
    return ToolContext(
        project_root=tmp_path, artifacts_dir=artifacts,
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


def test_git_diff_in_clean_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    out = get_registry()["git_diff"].invoke(_ctx(tmp_path), {"staged": False})
    assert out == "" or "no changes" in out.lower() or out.strip() == ""
