from __future__ import annotations

from pathlib import Path

from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine.change import Change
from tests.conftest import _seed_control


def _ctx(ch: Change) -> ToolContext:
    ctx = ToolContext(project_root=ch.project_root,
                      events_path=ch.events_path)
    ctx.event_log = ch.event_log()
    ctx.agent_id = "step:implement/task-lead"
    return ctx


def _change(tmp_path: Path) -> Change:
    _seed_control(tmp_path)
    return Change.create(tmp_path, title="t", change_id="aaa111", base_branch="main")


def test_tool_is_registered() -> None:
    assert "update_step_label" in get_registry()


def test_update_emits_event_with_opaque_string(tmp_path: Path) -> None:
    ch = _change(tmp_path)
    tool = get_registry()["update_step_label"]
    tool.invoke(_ctx(ch), {"label": "implementing · plan 2/4 · task 3/6"})

    emitted = [e for e in ch.event_log().read()
               if e["type"] == "step_label_updated"]
    assert emitted
    assert emitted[-1]["payload"]["label"] == "implementing · plan 2/4 · task 3/6"
    assert emitted[-1]["agent_id"] == "step:implement/task-lead"
