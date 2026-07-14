from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine.loader import TOP_LEVEL_CAPS
from imp.engine.quest import Quest
from imp.engine.state import State
from tests.conftest import _git, _seed_control


def _make(tmp_path: Path) -> tuple[Quest, State]:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Parent",
                       quest_id="2026-06-16-par111", base_branch="main")
    return ch, State.load(ch.state_dir)


def _ctx(ch: Quest, state: State | None) -> ToolContext:
    ctx = ToolContext(project_root=ch.project_root,
                      events_path=ch.events_path)
    ctx.quest = ch
    ctx.quest_state = state
    ctx.event_log = ch.event_log()
    ctx.agent_id = "step:finalize"
    return ctx


def test_registered_and_top_level_only() -> None:
    assert "create_quest" in get_registry()
    assert "create_quest" in TOP_LEVEL_CAPS


def test_create_quest_writes_quest_home_and_emits_event(tmp_path: Path) -> None:
    ch, state = _make(tmp_path)
    ctx = _ctx(ch, state)
    tool = get_registry()["create_quest"]
    msg = tool.invoke(ctx, {
        "title": "Follow-up: extract helper",
        "description": "extract the duplicated parser into one function",
        "depends_on": [ch.id]})

    new_ids = [p.name for p in (ch.control_root / ".imp" / "quests").iterdir()
               if p.name != ch.id]
    assert len(new_ids) == 1
    new_id = new_ids[0]
    assert new_id in msg

    created = Quest.load(ch.control_root, new_id)
    assert created.materialized is False
    assert created.origin == "backlog"
    assert created.title == "Follow-up: extract helper"
    assert created.seed_opening == (
        "Follow-up: extract helper\n\n"
        "extract the duplicated parser into one function")
    meta = json.loads((created.quest_dir / "quest.json").read_text())
    assert meta["depends_on"] == [ch.id]

    parent_events = [e for e in ch.event_log().read()
                     if e["type"] == "quest_created"
                     and e["payload"].get("kind") == "backlog"]
    assert len(parent_events) == 1
    assert parent_events[0]["payload"]["id"] == new_id
    assert parent_events[0]["payload"]["title"] == "Follow-up: extract helper"
    assert parent_events[0]["payload"]["depends_on"] == [ch.id]


def test_create_quest_defaults_no_deps(tmp_path: Path) -> None:
    ch, state = _make(tmp_path)
    tool = get_registry()["create_quest"]
    tool.invoke(_ctx(ch, state), {"title": "T", "description": "b"})
    new_id = next(p.name for p in (ch.control_root / ".imp" / "quests").iterdir()
                  if p.name != ch.id)
    meta = json.loads((Quest.load(ch.control_root, new_id).quest_dir
                       / "quest.json").read_text())
    assert meta["depends_on"] == []


def test_create_quest_from_subagent_raises(tmp_path: Path) -> None:
    ch, _ = _make(tmp_path)
    tool = get_registry()["create_quest"]
    with pytest.raises(RuntimeError):
        tool.invoke(_ctx(ch, None), {"title": "T", "description": "b"})  # subagent: state=None


def test_create_quest_avoids_live_cvcv_stem(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # End-to-end mint guard: the tool must pass the live-stem avoid-set to
    # new_quest_id, so a minted backlog id never reuses a live CVCV stem. A
    # site reverting to a bare new_quest_id() would fail this.
    from imp.engine import quest_id

    ch, state = _make(tmp_path)
    home = ch.control_root / ".imp" / "quests" / "kobe10"
    home.mkdir(parents=True)
    (home / "quest.json").write_text(json.dumps({"id": "kobe10", "title": "Seed"}))

    # First stem draw collides with the seeded live stem; avoidance must
    # resample past it to the next (distinct) stem.
    stems = iter(["kobe", "mira", "mira"])
    monkeypatch.setattr(quest_id, "_draw_clean_stem", lambda rng: next(stems))

    get_registry()["create_quest"].invoke(
        _ctx(ch, state), {"title": "T", "description": "b"})

    new_id = next(p.name for p in (ch.control_root / ".imp" / "quests").iterdir()
                  if p.name not in {ch.id, "kobe10"})
    assert new_id[:4] == "mira"


def test_create_quest_defers_seed_and_survives_skew_workflow(tmp_path: Path) -> None:
    # The parent is created over a VALID workflow; then the on-disk workflow is
    # broken (ungrantable allow_tools). create_quest must still record the backlog
    # stub — proof it passes defer_seed=True (an eager load would raise LoadError).
    ch, state = _make(tmp_path)
    agent = ch.control_root / "imp" / "workflows" / "imp" / "agents" / "stub.md"
    agent.write_text(
        "---\nname: stub\nallow_tools: [not_grantable]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\nbody\n")
    _git(ch.control_root, "add", "-A")
    _git(ch.control_root, "commit", "-qm", "skew")

    get_registry()["create_quest"].invoke(
        _ctx(ch, state), {"title": "T", "description": "b"})

    new_id = next(p.name for p in (ch.control_root / ".imp" / "quests").iterdir()
                  if p.name != ch.id)
    created = Quest.load(ch.control_root, new_id)
    assert created.origin == "backlog"
    seeded = State.load(created.state_dir)
    assert seeded.step is None
    assert seeded.has_started is False


def test_start_true_creates_active_draft_and_spawns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.remote.daemon import spawn

    calls: list[list[str]] = []
    monkeypatch.setattr(
        spawn, "_spawn_detached",
        lambda argv, env, cwd: calls.append(argv))

    ch, state = _make(tmp_path)
    msg = get_registry()["create_quest"].invoke(_ctx(ch, state), {
        "title": "Record a preference",
        "description": "prefer tabs over spaces",
        "workflow": "memory", "start": True})

    new_id = next(p.name for p in (ch.control_root / ".imp" / "quests").iterdir()
                  if p.name != ch.id)
    created = Quest.load(ch.control_root, new_id)
    assert created.origin == "draft"
    meta = json.loads((created.quest_dir / "quest.json").read_text())
    assert meta["workflow"] == "memory"

    assert calls == [[
        sys.executable, "-m", "imp", "resume", new_id,
        "--non-interactive", "--autonomous"]]
    assert new_id in msg
    assert "started" in msg.lower()

    child = next(e for e in ch.event_log().read()
                 if e["type"] == "quest_created"
                 and e["payload"].get("id") == new_id)
    assert child["payload"]["kind"] == "started"


def test_start_false_records_backlog_and_does_not_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.remote.daemon import spawn

    calls: list[list[str]] = []
    monkeypatch.setattr(
        spawn, "_spawn_detached",
        lambda argv, env, cwd: calls.append(argv))

    ch, state = _make(tmp_path)
    get_registry()["create_quest"].invoke(_ctx(ch, state), {
        "title": "T", "description": "b"})

    new_id = next(p.name for p in (ch.control_root / ".imp" / "quests").iterdir()
                  if p.name != ch.id)
    created = Quest.load(ch.control_root, new_id)
    assert created.origin == "backlog"
    assert calls == []

    child = next(e for e in ch.event_log().read()
                 if e["type"] == "quest_created"
                 and e["payload"].get("id") == new_id)
    assert child["payload"]["kind"] == "backlog"
