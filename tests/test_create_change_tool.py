from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
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
    ctx = ToolContext(project_root=ch.project_root,
                      events_path=ch.events_path)
    ctx.change = ch
    ctx.change_state = state
    ctx.event_log = ch.event_log()
    ctx.agent_id = "step:finalize"
    return ctx


def test_registered_and_top_level_only() -> None:
    assert "create_change" in get_registry()
    assert "create_change" in TOP_LEVEL_CAPS


def test_create_change_writes_change_home_and_emits_event(tmp_path: Path) -> None:
    ch, state = _make(tmp_path)
    ctx = _ctx(ch, state)
    tool = get_registry()["create_change"]
    msg = tool.invoke(ctx, {
        "title": "Follow-up: extract helper",
        "description": "extract the duplicated parser into one function",
        "depends_on": [ch.id]})

    new_ids = [p.name for p in (ch.control_root / ".imp" / "changes").iterdir()
               if p.name != ch.id]
    assert len(new_ids) == 1
    new_id = new_ids[0]
    assert new_id in msg

    created = Change.load(ch.control_root, new_id)
    assert created.materialized is False
    assert created.origin == "followup"
    assert created.title == "Follow-up: extract helper"
    assert created.seed_opening == (
        "Follow-up: extract helper\n\n"
        "extract the duplicated parser into one function")
    meta = json.loads((created.change_dir / "change.json").read_text())
    assert meta["depends_on"] == [ch.id]

    parent_events = [e for e in ch.event_log().read()
                     if e["type"] == "change_created"
                     and e["payload"].get("kind") == "backlog"]
    assert len(parent_events) == 1
    assert parent_events[0]["payload"]["id"] == new_id
    assert parent_events[0]["payload"]["title"] == "Follow-up: extract helper"
    assert parent_events[0]["payload"]["depends_on"] == [ch.id]


def test_create_change_defaults_no_deps(tmp_path: Path) -> None:
    ch, state = _make(tmp_path)
    tool = get_registry()["create_change"]
    tool.invoke(_ctx(ch, state), {"title": "T", "description": "b"})
    new_id = next(p.name for p in (ch.control_root / ".imp" / "changes").iterdir()
                  if p.name != ch.id)
    meta = json.loads((Change.load(ch.control_root, new_id).change_dir
                       / "change.json").read_text())
    assert meta["depends_on"] == []


def test_create_change_from_subagent_raises(tmp_path: Path) -> None:
    ch, _ = _make(tmp_path)
    tool = get_registry()["create_change"]
    with pytest.raises(RuntimeError):
        tool.invoke(_ctx(ch, None), {"title": "T", "description": "b"})  # subagent: state=None


def test_create_change_avoids_live_cvcv_stem(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # End-to-end mint guard: the tool must pass the live-stem avoid-set to
    # new_change_id, so a minted backlog id never reuses a live CVCV stem. A
    # site reverting to a bare new_change_id() would fail this.
    from imp.engine import change_id

    ch, state = _make(tmp_path)
    home = ch.control_root / ".imp" / "changes" / "kobe10"
    home.mkdir(parents=True)
    (home / "change.json").write_text(json.dumps({"id": "kobe10", "title": "Seed"}))

    # First stem draw collides with the seeded live stem; avoidance must
    # resample past it to the next (distinct) stem.
    stems = iter(["kobe", "mira", "mira"])
    monkeypatch.setattr(change_id, "_draw_clean_stem", lambda: next(stems))

    get_registry()["create_change"].invoke(
        _ctx(ch, state), {"title": "T", "description": "b"})

    new_id = next(p.name for p in (ch.control_root / ".imp" / "changes").iterdir()
                  if p.name not in {ch.id, "kobe10"})
    assert new_id[:4] == "mira"
