import pytest

from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine.state import State


def _ctx(tmp_path, state):
    ctx = ToolContext(project_root=tmp_path, fs_permissions=(), events_path=tmp_path / "e")
    ctx.change_state = state
    return ctx


def test_rename_change_records_pending(tmp_path) -> None:
    state = State(slug="old", step="s", step_attempt=0, status="running")
    tool = get_registry()["rename_change"]
    msg = tool.invoke(_ctx(tmp_path, state), {"name": "Add OAuth"})
    assert state.pending_rename == "Add OAuth"
    assert "next step boundary" in msg.lower()


def test_rename_change_rejects_empty(tmp_path) -> None:
    state = State(slug="old", step="s", step_attempt=0, status="running")
    tool = get_registry()["rename_change"]
    with pytest.raises(ValueError):
        tool.invoke(_ctx(tmp_path, state), {"name": "!!!"})
