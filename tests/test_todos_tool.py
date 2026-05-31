# tests/test_todos_tool.py
from __future__ import annotations

from pathlib import Path

import imp.agents.tools.todos  # noqa: F401
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        project_root=tmp_path,
        fs_permissions=(), events_path=tmp_path / "events.jsonl",
    )


def test_write_todos_replaces_list(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    out = get_registry()["write_todos"].invoke(ctx, {
        "todos": [{"text": "a", "status": "pending"}, {"text": "b", "status": "done"}],
    })
    assert ctx.todos == [{"text": "a", "status": "pending"}, {"text": "b", "status": "done"}]
    assert "2 todos" in out
