from __future__ import annotations

import json
from pathlib import Path

from imp.cli.store import _load_events_for_id
from imp.engine.quest import LAYOUT_VERSION, Quest
from tests.conftest import _seed_control, held_lock


def _seed_v11(control: Path, cid: str) -> Quest:
    q = Quest.create(control, title="T", quest_id=cid, base_branch="main")
    (q.state_dir / "state.json").write_text(json.dumps({
        "id": cid, "step": "code", "step_attempt": 0, "status": "paused",
        "resume_pending": "code", "resume_mode": "continue",
        "vars": {}, "loop_counts": {}, "current_step": None, "retry_feedback": None,
    }, indent=2, sort_keys=True))
    meta_path = q.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 11
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return q


def _stored_version(quest_dir: Path) -> int:
    return int(json.loads((quest_dir / "quest.json").read_text())["layout_version"])


def test_live_stale_home_reads_events_raw_without_migrating(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    q = _seed_v11(tmp_path, "livee")
    with held_lock(q.quest_dir / "lock"):
        events = _load_events_for_id(tmp_path, "livee")
        assert events is not None
        assert any(e.get("type") == "quest_created" for e in events)
        assert _stored_version(q.quest_dir) == 11
    assert _stored_version(q.quest_dir) == 11


def test_newer_live_home_reads_events_raw(tmp_path: Path) -> None:
    """Older imp (this install) reading a NEWER live quest (stored > engine):
    the identical held-lock path must read events raw, never migrate/downgrade."""
    _seed_control(tmp_path)
    q = Quest.create(tmp_path, title="T", quest_id="newer", base_branch="main")
    meta_path = q.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = LAYOUT_VERSION + 1
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    with held_lock(q.quest_dir / "lock"):
        events = _load_events_for_id(tmp_path, "newer")
        assert events is not None
        assert any(e.get("type") == "quest_created" for e in events)
        assert _stored_version(q.quest_dir) == LAYOUT_VERSION + 1
    assert _stored_version(q.quest_dir) == LAYOUT_VERSION + 1


def test_absent_id_still_returns_none(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    assert _load_events_for_id(tmp_path, "nosuch") is None
