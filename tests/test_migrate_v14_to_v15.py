from __future__ import annotations

import json
from pathlib import Path

from imp.engine.quest import LAYOUT_VERSION, Quest, _migrate_v14_to_v15
from tests.conftest import _seed_control


def _seed_v14(control: Path, cid: str) -> Path:
    """A live home stamped back to v14: the vars key stripped from quest.json,
    exactly as a home written before the field existed."""
    ch = Quest.create(control, title="", quest_id=cid, base_branch="main")
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta.pop("vars", None)
    meta["layout_version"] = 14
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return ch.quest_dir


def _meta(quest_dir: Path) -> dict:
    return json.loads((quest_dir / "quest.json").read_text())


def test_hop_backfills_empty_vars_and_stamps_the_version(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v14(tmp_path, "mv1")
    _migrate_v14_to_v15(qd)
    assert _meta(qd)["vars"] == {}
    assert _meta(qd)["layout_version"] == 15


def test_hop_preserves_overrides_already_present(tmp_path: Path) -> None:
    # A home whose vars were written before an interrupted stamp keeps them:
    # the backfill fills an absent key only, so the re-run is lossless.
    _seed_control(tmp_path)
    qd = _seed_v14(tmp_path, "mv2")
    meta = _meta(qd)
    meta["vars"] = {"n": 5}
    (qd / "quest.json").write_text(json.dumps(meta, indent=2, sort_keys=True))
    _migrate_v14_to_v15(qd)
    assert _meta(qd)["vars"] == {"n": 5}
    assert _meta(qd)["layout_version"] == 15


def test_hop_is_idempotent_and_leaves_no_temp_file(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v14(tmp_path, "mv3")
    _migrate_v14_to_v15(qd)
    _migrate_v14_to_v15(qd)                # second run must not corrupt or re-change
    assert _meta(qd)["vars"] == {}
    assert _meta(qd)["layout_version"] == 15
    assert not list(qd.glob(".quest-*"))   # atomic rewrite cleaned up after itself


def test_load_migrates_a_v14_home_end_to_end(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v14(tmp_path, "mv4")
    quest = Quest.load(tmp_path, "mv4")    # triggers the migration chain on load
    assert quest.vars == {}
    assert _meta(qd)["layout_version"] == LAYOUT_VERSION
    assert LAYOUT_VERSION == 16
