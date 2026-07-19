from __future__ import annotations

import json
from pathlib import Path

from imp.engine.quest import LAYOUT_VERSION, Quest, _migrate_v6_to_v7
from imp.engine.state import State
from tests.conftest import _seed_control

_EVENTS = [
    {"id": "q0", "parent_id": None, "agent_id": None, "timestamp": "2026-06-01T00:00:00+00:00",
     "type": "quest_created", "payload": {"id": "x", "title": ""}},
    {"id": "u1", "parent_id": None, "agent_id": "A", "timestamp": "2026-06-01T01:00:00+00:00",
     "type": "user_message",
     "payload": {"role": "user", "subtype": "intent",
                 "content": [{"type": "text", "text": "hi"}]}},
]


def _quest_dir(control: Path, cid: str) -> Path:
    return control / ".imp" / "quests" / cid


def _seed_v6_quest(control: Path, cid: str, events: list[dict]) -> Quest:
    """A live home with an events.jsonl, state.json (started_at unset), stamped v6."""
    ch = Quest.create(control, title="", quest_id=cid, base_branch="main")
    ch.events_path.write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 6
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return ch


def test_hop_backfills_started_at_from_first_user_message(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _seed_v6_quest(tmp_path, "mig701", _EVENTS)

    _migrate_v6_to_v7(ch.quest_dir)

    assert State.load(ch.state_dir).started_at == "2026-06-01T01:00:00+00:00"
    meta = json.loads((ch.quest_dir / "quest.json").read_text())
    assert meta["layout_version"] == 7


def test_hop_falls_back_to_created_at_when_no_events(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _seed_v6_quest(tmp_path, "mig702", [])
    created_at = json.loads((ch.quest_dir / "quest.json").read_text())["created_at"]

    _migrate_v6_to_v7(ch.quest_dir)

    assert State.load(ch.state_dir).started_at == created_at


def test_hop_skips_backfill_when_no_state_json_but_still_stamps(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _seed_v6_quest(tmp_path, "mig703", _EVENTS)
    (ch.state_dir / "state.json").unlink()          # un-materialized seed home

    _migrate_v6_to_v7(ch.quest_dir)                 # must not raise

    assert not (ch.state_dir / "state.json").exists()
    meta = json.loads((ch.quest_dir / "quest.json").read_text())
    assert meta["layout_version"] == 7


def test_load_migrates_and_stamps_v7(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _seed_v6_quest(tmp_path, "mig704", _EVENTS)

    Quest.load(tmp_path, "mig704")                  # load() triggers the hop chain

    meta = json.loads((_quest_dir(tmp_path, "mig704") / "quest.json").read_text())
    assert meta["layout_version"] == LAYOUT_VERSION
    started = State.load(_quest_dir(tmp_path, "mig704") / "state").started_at
    assert started == "2026-06-01T01:00:00+00:00"
