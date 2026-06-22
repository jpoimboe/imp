from __future__ import annotations

from pathlib import Path

from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine.change import Change
from tests.conftest import _seed_control

NOTICE = "plan 2/4 · 2 tasks\n   1. add widget\n   2. wire widget"


def _ctx(ch: Change) -> ToolContext:
    ctx = ToolContext(project_root=ch.project_root, fs_permissions=(),
                      events_path=ch.events_path)
    ctx.event_log = ch.event_log()
    ctx.agent_id = "step:plan/plan-finalized"
    return ctx


def _change(tmp_path: Path) -> Change:
    _seed_control(tmp_path)
    return Change.create(tmp_path, title="t", change_id="aaa111", base_branch="main")


def test_tool_is_registered() -> None:
    assert "emit_notice" in get_registry()


def test_emit_notice_carries_opaque_text(tmp_path: Path) -> None:
    ch = _change(tmp_path)
    tool = get_registry()["emit_notice"]
    tool.invoke(_ctx(ch), {"text": NOTICE})

    emitted = [e for e in ch.event_log().read() if e["type"] == "notice"]
    assert emitted
    assert emitted[-1]["payload"]["text"] == NOTICE
