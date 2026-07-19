from __future__ import annotations

import json
from pathlib import Path

from imp.engine.quest import LAYOUT_VERSION, Quest, _migrate_v5_to_v6
from tests.conftest import _seed_control

_OLD_EVENTS = [
    {"id": "i0", "parent_id": None, "agent_id": None, "timestamp": "T0",
     "type": "opening_intent", "payload": {"text": "Fix the login bug"}},
    {"id": "u1", "parent_id": "i0", "agent_id": "A", "timestamp": "T1",
     "type": "user_message",
     "payload": {"role": "user", "content": [{"type": "text", "text": "hi"}],
                 "human": True}},
    {"id": "r1", "parent_id": "u1", "agent_id": "A", "timestamp": "T2",
     "type": "llm_response",
     "payload": {"provider": "p", "model": "m",
                 "raw": {"content": [{"type": "text", "text": "ok"}]},
                 "usage": {"input_tokens": 1}, "stop_reason": "end_turn"}},
]


def _quest_dir(control: Path, cid: str) -> Path:
    return control / ".imp" / "quests" / cid


def _seed_old_quest(control: Path, cid: str) -> Quest:
    """A live home carrying an old-format events.jsonl, stamped back to v5."""
    ch = Quest.create(control, title="", quest_id=cid, base_branch="main")
    ch.events_path.write_text(
        "".join(json.dumps(e) + "\n" for e in _OLD_EVENTS), encoding="utf-8")
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 5
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return ch


def test_load_migrates_events_and_stamps_current_version(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _seed_old_quest(tmp_path, "mig001")

    Quest.load(tmp_path, "mig001")

    meta = json.loads((_quest_dir(tmp_path, "mig001") / "quest.json").read_text())
    assert meta["layout_version"] == LAYOUT_VERSION == 7
    lines = (_quest_dir(tmp_path, "mig001") / "events.jsonl").read_text().splitlines()
    migrated = [json.loads(ln) for ln in lines if ln.strip()]
    assert [e["type"] for e in migrated] == [
        "user_message", "user_message", "agent_message"]
    assert migrated[0]["payload"]["subtype"] == "intent"
    assert migrated[1]["payload"]["subtype"] == "reply"
    assert "human" not in migrated[1]["payload"]


def test_second_load_is_a_no_op(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _seed_old_quest(tmp_path, "mig002")
    events_file = _quest_dir(tmp_path, "mig002") / "events.jsonl"

    Quest.load(tmp_path, "mig002")
    after_first = events_file.read_bytes()
    Quest.load(tmp_path, "mig002")

    assert events_file.read_bytes() == after_first
    meta = json.loads((_quest_dir(tmp_path, "mig002") / "quest.json").read_text())
    assert meta["layout_version"] == 7


def test_migration_leaves_no_temp_and_writes_compact_lines(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _seed_old_quest(tmp_path, "mig003")

    _migrate_v5_to_v6(ch.quest_dir)

    assert not list(ch.quest_dir.glob(".events-*"))
    for ln in ch.events_path.read_text().splitlines():
        # One compact JSON object per line (no indentation, no intra-record newlines).
        assert ln == json.dumps(json.loads(ln))


def test_migration_without_events_file_still_stamps_v6(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="", quest_id="mig004", base_branch="main")
    ch.events_path.unlink()
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 5
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))

    _migrate_v5_to_v6(ch.quest_dir)

    assert json.loads(meta_path.read_text())["layout_version"] == 6
    assert not ch.events_path.exists()
