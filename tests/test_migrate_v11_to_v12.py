from __future__ import annotations

import json
from pathlib import Path

from imp.engine.quest import LAYOUT_VERSION, Quest, _migrate_v11_to_v12
from tests.conftest import _seed_control


def _seed_v11(control: Path, cid: str, *, resume_mode) -> Path:
    """A live home stamped back to v11 with a hand-written state.json carrying the
    pre-rename resume_mode vocabulary. `resume_mode` may be "continue"/"solicit"/None."""
    ch = Quest.create(control, title="", quest_id=cid, base_branch="main")
    (ch.state_dir / "state.json").write_text(json.dumps({
        "id": cid, "step": "code", "step_attempt": 0, "status": "paused",
        "resume_pending": "code", "resume_mode": resume_mode,
        "vars": {}, "loop_counts": {}, "current_step": None, "retry_feedback": None,
    }, indent=2, sort_keys=True))
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 11
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return ch.quest_dir


def _state(quest_dir: Path) -> dict:
    return json.loads((quest_dir / "state" / "state.json").read_text())


def _meta(quest_dir: Path) -> dict:
    return json.loads((quest_dir / "quest.json").read_text())


def test_continue_becomes_true(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v11(tmp_path, "mw1", resume_mode="continue")
    _migrate_v11_to_v12(qd)
    st = _state(qd)
    assert st["resume_should_continue"] is True
    assert "resume_mode" not in st
    assert _meta(qd)["layout_version"] == 12


def test_solicit_becomes_false(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v11(tmp_path, "mw2", resume_mode="solicit")
    _migrate_v11_to_v12(qd)
    st = _state(qd)
    assert st["resume_should_continue"] is False
    assert "resume_mode" not in st
    assert _meta(qd)["layout_version"] == 12


def test_null_becomes_false(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v11(tmp_path, "mw3", resume_mode=None)
    _migrate_v11_to_v12(qd)
    st = _state(qd)
    assert st["resume_should_continue"] is False
    assert "resume_mode" not in st
    assert _meta(qd)["layout_version"] == 12


def test_unmaterialized_seed_advances_version_only(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v11(tmp_path, "mw4", resume_mode="continue")
    (qd / "state" / "state.json").unlink()   # un-materialized: no state.json
    _migrate_v11_to_v12(qd)
    assert _meta(qd)["layout_version"] == 12
    assert not (qd / "state" / "state.json").exists()


def test_load_migrates_v11_home_end_to_end(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v11(tmp_path, "mw5", resume_mode="continue")
    Quest.load(tmp_path, "mw5")               # triggers the migration chain on load
    st = _state(qd)
    assert st["resume_should_continue"] is True
    assert "resume_mode" not in st
    assert _meta(qd)["layout_version"] == LAYOUT_VERSION


def test_hop_is_idempotent_and_pins_layout_version(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v11(tmp_path, "mw6", resume_mode="continue")
    _migrate_v11_to_v12(qd)
    _migrate_v11_to_v12(qd)                   # second run must not corrupt or re-change
    st = _state(qd)
    assert st["resume_should_continue"] is True
    assert "resume_mode" not in st
    assert not list((qd / "state").glob(".state-*")) and not list(qd.glob(".quest-*"))
    assert LAYOUT_VERSION == 21
