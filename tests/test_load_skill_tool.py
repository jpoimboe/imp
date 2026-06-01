# tests/test_load_skill_tool.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.agents.runner import ChangeContext
from imp.agents.skills import SkillNameError, SkillNotFound
from imp.agents.tools.context import ToolContext
from imp.agents.tools.load_skill import make_load_skill_tool
from imp.engine.state import JsonlEventLog


def _ctx(tmp_path: Path) -> ToolContext:
    wf = tmp_path / ".imp" / "change" / "workflow"
    (wf / "skills").mkdir(parents=True)
    (wf / "skills" / "refactor.md").write_text("# Refactor\ndo the thing\n")
    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
    )
    ctx.change_context = ChangeContext(
        slug="s", title="t", project_root=tmp_path, workflow_dir=wf,
    )
    ctx.event_log = JsonlEventLog(tmp_path / "events.jsonl")
    return ctx


def test_load_skill_returns_body(tmp_path: Path) -> None:
    tool = make_load_skill_tool()
    out = tool.invoke(_ctx(tmp_path), {"name": "refactor"})
    assert out == "# Refactor\ndo the thing\n"


def test_load_skill_emits_skill_load_event(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    make_load_skill_tool().invoke(ctx, {"name": "refactor"})
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    loads = [e for e in events if e["type"] == "skill_load"]
    assert len(loads) == 1
    assert loads[0]["payload"] == {"name": "refactor", "source": "agent"}
    assert loads[0]["agent_id"] is None


def test_load_skill_missing_name_raises_not_found(tmp_path: Path) -> None:
    with pytest.raises(SkillNotFound):
        make_load_skill_tool().invoke(_ctx(tmp_path), {"name": "ghost"})


def test_load_skill_traversal_raises_name_error(tmp_path: Path) -> None:
    with pytest.raises(SkillNameError):
        make_load_skill_tool().invoke(_ctx(tmp_path), {"name": "../config"})


def test_load_skill_no_event_when_not_found(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(SkillNotFound):
        make_load_skill_tool().invoke(ctx, {"name": "ghost"})
    text = (tmp_path / "events.jsonl").read_text() if (tmp_path / "events.jsonl").exists() else ""
    assert "skill_load" not in text


def test_load_skill_schema_declares_name(tmp_path: Path) -> None:
    tool = make_load_skill_tool()
    assert tool.name == "load_skill"
    assert tool.input_schema["required"] == ["name"]
