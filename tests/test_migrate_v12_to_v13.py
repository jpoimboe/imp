from __future__ import annotations

import json
from pathlib import Path

from imp.engine.quest import LAYOUT_VERSION, Quest, _migrate_v12_to_v13
from tests.conftest import _seed_control


def _seed_v12(control: Path, cid: str, *, workflow) -> Path:
    """A live home stamped back to v12 with an explicit `workflow` in quest.json."""
    ch = Quest.create(control, title="", quest_id=cid, base_branch="main")
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 12
    meta["workflow"] = workflow
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return ch.quest_dir


def _meta(quest_dir: Path) -> dict:
    return json.loads((quest_dir / "quest.json").read_text())


def test_explicit_imp_becomes_default(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v12(tmp_path, "mv1", workflow="imp")
    _migrate_v12_to_v13(qd)
    assert _meta(qd)["workflow"] == "default"
    assert _meta(qd)["layout_version"] == 13


def test_null_workflow_is_untouched(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v12(tmp_path, "mv2", workflow=None)
    _migrate_v12_to_v13(qd)
    assert _meta(qd)["workflow"] is None
    assert _meta(qd)["layout_version"] == 13


def test_other_workflow_is_untouched(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v12(tmp_path, "mv3", workflow="patent")
    _migrate_v12_to_v13(qd)
    assert _meta(qd)["workflow"] == "patent"
    assert _meta(qd)["layout_version"] == 13


def test_load_migrates_v12_home_end_to_end(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v12(tmp_path, "mv4", workflow="imp")
    Quest.load(tmp_path, "mv4")               # triggers the migration chain on load
    assert _meta(qd)["workflow"] == "default"
    assert _meta(qd)["layout_version"] == 13


def test_hop_is_idempotent_and_pins_layout_version(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v12(tmp_path, "mv5", workflow="imp")
    _migrate_v12_to_v13(qd)
    _migrate_v12_to_v13(qd)                   # second run must not corrupt or re-change
    assert _meta(qd)["workflow"] == "default"
    assert _meta(qd)["layout_version"] == 13
    assert not list(qd.glob(".quest-*"))
    assert LAYOUT_VERSION == 13
