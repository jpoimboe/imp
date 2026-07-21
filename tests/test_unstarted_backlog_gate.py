from __future__ import annotations

import json
from pathlib import Path

from imp.cli.main import _unstarted_backlog_meta
from imp.engine.quest import LAYOUT_VERSION, Quest
from tests.conftest import _seed_control, held_lock


def _seed_v11_draft(control: Path, cid: str) -> Quest:
    """An un-materialized (draft) home stamped back to layout v11."""
    q = Quest.create_draft(
        control, title="B", quest_id=cid, base_branch="main",
        origin="backlog", defer_seed=True)
    meta_path = q.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 11
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return q


def _stored_version(quest_dir: Path) -> int:
    return int(json.loads((quest_dir / "quest.json").read_text())["layout_version"])


def test_backlog_meta_does_not_migrate_a_live_stale_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    q = _seed_v11_draft(tmp_path, "bklog")
    with held_lock(q.quest_dir / "lock"):
        assert _unstarted_backlog_meta(tmp_path, "bklog") is None
        assert _stored_version(q.quest_dir) == 11
    assert _stored_version(q.quest_dir) == 11


def test_backlog_meta_migrates_an_idle_stale_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    q = _seed_v11_draft(tmp_path, "idlebk")
    meta = _unstarted_backlog_meta(tmp_path, "idlebk")
    assert meta is not None
    assert _stored_version(q.quest_dir) == LAYOUT_VERSION
