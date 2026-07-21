from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.engine.errors import LayoutMismatch
from imp.engine.quest import LAYOUT_VERSION, Quest
from tests.conftest import _seed_control, held_lock


def _seed_v11(control: Path, cid: str) -> Quest:
    """A materialized live home stamped back to layout v11 with the pre-rename
    resume_mode vocabulary, so the 11->12 hop has real work to do."""
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


def test_lockable_load_migrates_idle_stale_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    q = _seed_v11(tmp_path, "idle1")
    loaded = Quest.load(tmp_path, "idle1", migrate_if_lockable=True)
    assert loaded.id == "idle1"
    assert _stored_version(q.quest_dir) == LAYOUT_VERSION


def test_lockable_load_skips_migration_under_held_lock(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    q = _seed_v11(tmp_path, "live1")
    with held_lock(q.quest_dir / "lock"):
        with pytest.raises(LayoutMismatch):
            Quest.load(tmp_path, "live1", migrate_if_lockable=True)
        assert _stored_version(q.quest_dir) == 11
    assert _stored_version(q.quest_dir) == 11


def test_run_path_load_migrates_even_under_held_lock(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    q = _seed_v11(tmp_path, "run1")
    with held_lock(q.quest_dir / "lock"):
        loaded = Quest.load(tmp_path, "run1")  # default migrate_if_lockable=False
        assert loaded.id == "run1"
        assert _stored_version(q.quest_dir) == LAYOUT_VERSION
