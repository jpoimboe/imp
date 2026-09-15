from __future__ import annotations

import json
from pathlib import Path

from imp.cli.main import _unstarted_backlog_meta
from imp.engine.quest import LAYOUT_VERSION, Quest
from tests.conftest import _seed_control, held_lock


def _seed_draft(control: Path, cid: str) -> Quest:
    """An un-materialized (draft) Backlog home at the current layout version."""
    return Quest.create_draft(
        control, title="B", quest_id=cid, base_branch="main",
        origin="backlog", defer_seed=True)


def _stored_version(quest_dir: Path) -> int:
    return int(json.loads((quest_dir / "quest.json").read_text())["layout_version"])


def test_backlog_meta_is_none_for_a_live_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    q = _seed_draft(tmp_path, "bklog")
    with held_lock(q.quest_dir / "lock"):
        assert _unstarted_backlog_meta(tmp_path, "bklog") is None
        assert _stored_version(q.quest_dir) == LAYOUT_VERSION
    assert _stored_version(q.quest_dir) == LAYOUT_VERSION
