from __future__ import annotations

import json
from pathlib import Path

from imp.engine.quest import LAYOUT_VERSION, Quest, _migrate_v8_to_v9
from tests.conftest import _seed_control


def _seed_v8(control: Path, cid: str) -> Path:
    """A live home stamped back to v8 with the create_mode key stripped — the
    on-disk shape of a home created before this field existed."""
    ch = Quest.create(control, title="", quest_id=cid, base_branch="main")
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta.pop("create_mode", None)
    meta["layout_version"] = 8
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return ch.quest_dir


def _meta(quest_dir: Path) -> dict:
    return json.loads((quest_dir / "quest.json").read_text())


def test_hop_backfills_create_mode_new_and_stamps_v9(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v8(tmp_path, "mig901")
    _migrate_v8_to_v9(qd)
    assert _meta(qd)["create_mode"] == "new"
    assert _meta(qd)["layout_version"] == 9


def test_load_migrates_v8_home_and_reads_create_mode_new(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v8(tmp_path, "mig902")
    q = Quest.load(tmp_path, "mig902")
    assert q.create_mode == "new"
    assert _meta(qd)["layout_version"] == LAYOUT_VERSION


def test_hop_is_idempotent_and_leaves_no_temp_file(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v8(tmp_path, "mig903")
    _migrate_v8_to_v9(qd)
    _migrate_v8_to_v9(qd)  # second run must not corrupt or change the outcome
    assert _meta(qd)["create_mode"] == "new"
    assert _meta(qd)["layout_version"] == 9
    assert not list(qd.glob(".quest-*"))
    assert LAYOUT_VERSION == 15
