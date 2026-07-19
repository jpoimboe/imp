from __future__ import annotations

import json
from pathlib import Path

from imp.engine.quest import LAYOUT_VERSION, Quest, _migrate_v7_to_v8
from tests.conftest import _seed_control


def _quest_dir(control: Path, cid: str) -> Path:
    return control / ".imp" / "quests" / cid


def _seed_v7(control: Path, cid: str, *, status: str,
             paused_events: list[dict]) -> Path:
    """A live home stamped back to v7 with a hand-written state.json (no
    halt_condition key) and the given events.jsonl tail."""
    ch = Quest.create(control, title="", quest_id=cid, base_branch="main")
    (ch.state_dir / "state.json").write_text(json.dumps({
        "id": cid, "step": "w", "step_attempt": 0, "status": status,
        "vars": {}, "loop_counts": {}, "current_step": None,
        "retry_feedback": None,
    }, indent=2, sort_keys=True))
    ch.events_path.write_text(
        "".join(json.dumps(e) + "\n" for e in paused_events), encoding="utf-8")
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 7
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return ch.quest_dir


def _paused(reason: str, **extra: object) -> dict:
    return {"id": "p", "parent_id": None, "agent_id": None, "timestamp": "T",
            "type": "workflow_paused", "payload": {"reason": reason, **extra}}


def _halt(quest_dir: Path) -> object:
    return json.loads((quest_dir / "state" / "state.json").read_text()).get(
        "halt_condition")


def test_error_reason_backfills_error(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v7(tmp_path, "m1", status="paused",
                  paused_events=[_paused("internal_error")])
    Quest.load(tmp_path, "m1")
    assert _halt(qd) == "error"
    assert json.loads((qd / "quest.json").read_text())["layout_version"] == 8


def test_clean_reason_backfills_none(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v7(tmp_path, "m2", status="paused",
                  paused_events=[_paused("awaiting_user")])
    Quest.load(tmp_path, "m2")
    assert _halt(qd) is None
    assert json.loads((qd / "quest.json").read_text())["layout_version"] == 8


def test_interrupt_over_error_reason_backfills_none(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v7(tmp_path, "m3", status="paused",
                  paused_events=[_paused("internal_error", interrupt_reason="ctrl-c")])
    Quest.load(tmp_path, "m3")
    assert _halt(qd) is None


def test_non_paused_home_is_not_backfilled(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v7(tmp_path, "m4", status="new",
                  paused_events=[_paused("internal_error")])
    _migrate_v7_to_v8(qd)
    assert _halt(qd) is None
    assert json.loads((qd / "quest.json").read_text())["layout_version"] == 8


def test_migration_without_events_still_stamps_v8(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v7(tmp_path, "m5", status="paused", paused_events=[])
    qd_events = qd / "events.jsonl"
    if qd_events.exists():
        qd_events.unlink()
    _migrate_v7_to_v8(qd)
    assert _halt(qd) is None
    assert json.loads((qd / "quest.json").read_text())["layout_version"] == 8


def test_events_without_workflow_paused_backfills_none(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    non_pause_events = [
        {"id": "e1", "parent_id": None, "agent_id": None, "timestamp": "T",
         "type": "quest_created", "payload": {}},
        {"id": "e2", "parent_id": None, "agent_id": None, "timestamp": "T",
         "type": "step_started", "payload": {"step": "w"}},
    ]
    qd = _seed_v7(tmp_path, "m7", status="paused", paused_events=non_pause_events)
    _migrate_v7_to_v8(qd)
    assert _halt(qd) is None
    assert json.loads((qd / "quest.json").read_text())["layout_version"] == 8


def test_migration_leaves_no_temp_file(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v7(tmp_path, "m6", status="paused",
                  paused_events=[_paused("max_loops")])
    _migrate_v7_to_v8(qd)
    assert not list((qd / "state").glob(".state-*"))
    assert LAYOUT_VERSION == 8
