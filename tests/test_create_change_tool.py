from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine import backlog
from imp.engine.change import Change
from imp.engine.loader import TOP_LEVEL_CAPS
from imp.engine.state import State
from tests.conftest import _seed_control


def _make(tmp_path: Path) -> tuple[Change, State]:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Parent",
                       change_id="2026-06-16-par111", base_branch="main")
    return ch, State.load(ch.state_dir)


def _ctx(ch: Change, state: State | None) -> ToolContext:
    ctx = ToolContext(project_root=ch.project_root, fs_permissions=(),
                      events_path=ch.events_path)
    ctx.change = ch
    ctx.change_state = state
    ctx.event_log = ch.event_log()
    ctx.agent_id = "step:finalize"
    return ctx


def test_registered_and_top_level_only() -> None:
    assert "create_change" in get_registry()
    assert "create_change" in TOP_LEVEL_CAPS


def test_create_change_writes_record_and_emits_event(tmp_path: Path) -> None:
    ch, state = _make(tmp_path)
    ctx = _ctx(ch, state)
    tool = get_registry()["create_change"]
    msg = tool.invoke(ctx, {
        "title": "Follow-up: extract helper",
        "brief": "extract the duplicated parser into one function",
        "depends_on": [ch.id]})

    # Returned confirmation names the new id.
    rec_ids = backlog.list_ids(ch.control_root)
    assert len(rec_ids) == 1
    new_id = rec_ids[0]
    assert new_id in msg
    rec = backlog.read_record(ch.control_root, new_id)
    assert rec["title"] == "Follow-up: extract helper"
    assert rec["brief"] == "extract the duplicated parser into one function"
    assert rec["depends_on"] == [ch.id]
    assert rec["created_by"] == "step:finalize"

    created = [e for e in ch.event_log().read()
               if e["type"] == "change_created"
               and e["payload"].get("kind") == "backlog"]
    assert len(created) == 1
    assert created[0]["payload"]["id"] == new_id


def test_create_change_defaults_no_deps(tmp_path: Path) -> None:
    ch, state = _make(tmp_path)
    tool = get_registry()["create_change"]
    tool.invoke(_ctx(ch, state), {"title": "T", "brief": "b"})
    rec = backlog.read_record(ch.control_root, backlog.list_ids(ch.control_root)[0])
    assert rec["depends_on"] == []


def test_create_change_from_subagent_raises(tmp_path: Path) -> None:
    ch, _ = _make(tmp_path)
    tool = get_registry()["create_change"]
    with pytest.raises(RuntimeError):
        tool.invoke(_ctx(ch, None), {"title": "T", "brief": "b"})  # subagent: state=None
