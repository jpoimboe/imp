import json
from pathlib import Path

import pytest

from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _seed_control


def _ctx(ch: Change, state: State) -> ToolContext:
    ctx = ToolContext(project_root=ch.project_root, fs_permissions=(),
                      events_path=ch.events_path)
    ctx.change = ch
    ctx.change_state = state
    ctx.event_log = ch.event_log()
    return ctx


def _make(tmp_path: Path, title: str = "") -> tuple[Change, State]:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title=title,
                       change_id="2026-06-05-bbb111", base_branch="main")
    return ch, State.load(ch.state_dir)


def test_rename_applies_immediately_and_returns_slug(tmp_path: Path) -> None:
    ch, state = _make(tmp_path)
    tool = get_registry()["rename_change"]
    msg = tool.invoke(_ctx(ch, state), {"name": "Add OAuth"})

    assert ch.title == "Add OAuth"
    assert ch.slug == "2026-06-05-add-oauth"
    assert "2026-06-05-add-oauth" in msg
    assert "imp/specs/2026-06-05-add-oauth.md" in msg
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert meta["title"] == "Add OAuth"
    renamed = [e for e in ch.event_log().read() if e["type"] == "change_renamed"]
    assert renamed and renamed[-1]["payload"]["new_title"] == "Add OAuth"


def test_rename_noop_when_slug_unchanged(tmp_path: Path) -> None:
    ch, state = _make(tmp_path, title="Add OAuth")
    tool = get_registry()["rename_change"]
    # A different title that slugifies identically must NOT churn (slug equality).
    msg = tool.invoke(_ctx(ch, state), {"name": "add  oauth"})
    assert "already named" in msg.lower()
    assert [e for e in ch.event_log().read() if e["type"] == "change_renamed"] == []


def test_double_rename_compares_against_live_slug(tmp_path: Path) -> None:
    ch, state = _make(tmp_path)
    ctx = _ctx(ch, state)
    tool = get_registry()["rename_change"]
    tool.invoke(ctx, {"name": "First Name"})
    msg2 = tool.invoke(ctx, {"name": "first name"})   # same slug as the FIRST call
    assert "already named" in msg2.lower()
    assert ch.slug == "2026-06-05-first-name"
    emitted = [e for e in ch.event_log().read() if e["type"] == "change_renamed"]
    assert len(emitted) == 1                          # only the first applied


def test_rename_rejects_empty(tmp_path: Path) -> None:
    ch, state = _make(tmp_path)
    tool = get_registry()["rename_change"]
    with pytest.raises(ValueError):
        tool.invoke(_ctx(ch, state), {"name": "!!!"})


def test_rename_from_subagent_raises(tmp_path: Path) -> None:
    ch, _ = _make(tmp_path)
    ctx = ToolContext(project_root=ch.project_root, fs_permissions=(),
                      events_path=ch.events_path)
    ctx.change = ch
    ctx.change_state = None              # subagent: no rename authority
    tool = get_registry()["rename_change"]
    with pytest.raises(RuntimeError):
        tool.invoke(ctx, {"name": "Whatever"})
