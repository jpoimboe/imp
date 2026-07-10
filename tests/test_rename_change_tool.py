import json
from pathlib import Path

import pytest

from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _seed_control


def _ctx(ch: Change, state: State) -> ToolContext:
    ctx = ToolContext(project_root=ch.project_root,
                      events_path=ch.events_path)
    ctx.change = ch
    ctx.change_state = state
    ctx.event_log = ch.event_log()
    return ctx


def _make(tmp_path: Path, title: str = "") -> tuple[Change, State]:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title=title,
                       quest_id="bbb111", base_branch="main")
    return ch, State.load(ch.state_dir)


def test_rename_applies_immediately_and_returns_id(tmp_path: Path) -> None:
    ch, state = _make(tmp_path)
    tool = get_registry()["rename_change"]
    msg = tool.invoke(_ctx(ch, state), {"name": "Add version flag"})

    assert ch.title == "Add version flag"            # stored verbatim
    assert "bbb111" in msg                           # returns the id
    assert f"{ch.quest_dir}/artifacts/spec/bbb111.md" in msg
    meta = json.loads((ch.quest_dir / "change.json").read_text())
    assert meta["title"] == "Add version flag"
    renamed = [e for e in ch.event_log().read() if e["type"] == "change_renamed"]
    assert renamed and renamed[-1]["payload"]["new_title"] == "Add version flag"


def test_rename_strips_outer_whitespace(tmp_path: Path) -> None:
    ch, state = _make(tmp_path)
    tool = get_registry()["rename_change"]
    tool.invoke(_ctx(ch, state), {"name": "  Add OAuth  "})
    assert ch.title == "Add OAuth"


def test_rename_noop_when_title_unchanged(tmp_path: Path) -> None:
    ch, state = _make(tmp_path, title="Add OAuth")
    tool = get_registry()["rename_change"]
    # Same title after stripping outer whitespace must NOT churn.
    msg = tool.invoke(_ctx(ch, state), {"name": "  Add OAuth  "})
    assert "already named" in msg.lower()
    assert [e for e in ch.event_log().read() if e["type"] == "change_renamed"] == []


def test_double_rename_to_same_title_is_noop(tmp_path: Path) -> None:
    ch, state = _make(tmp_path)
    ctx = _ctx(ch, state)
    tool = get_registry()["rename_change"]
    tool.invoke(ctx, {"name": "First Title"})
    msg2 = tool.invoke(ctx, {"name": "First Title"})   # identical title
    assert "already named" in msg2.lower()
    assert ch.title == "First Title"
    emitted = [e for e in ch.event_log().read() if e["type"] == "change_renamed"]
    assert len(emitted) == 1                          # only the first applied


def test_rename_rejects_empty(tmp_path: Path) -> None:
    ch, state = _make(tmp_path)
    tool = get_registry()["rename_change"]
    with pytest.raises(ValueError):
        tool.invoke(_ctx(ch, state), {"name": "   "})   # whitespace-only → empty


def test_rename_description_drops_hardcoded_quest_dir() -> None:
    tool = get_registry()["rename_change"]
    assert ".imp/change" not in tool.description


def test_rename_from_subagent_raises(tmp_path: Path) -> None:
    ch, _ = _make(tmp_path)
    ctx = ToolContext(project_root=ch.project_root,
                      events_path=ch.events_path)
    ctx.change = ch
    ctx.change_state = None              # subagent: no rename authority
    tool = get_registry()["rename_change"]
    with pytest.raises(RuntimeError):
        tool.invoke(ctx, {"name": "Whatever"})
