from __future__ import annotations

import json
from pathlib import Path

from imp.engine.quest import LAYOUT_VERSION, Quest, _migrate_v9_to_v10
from tests.conftest import _seed_control


def _seed_v9(control: Path, cid: str, *, status: str,
             halt_condition: str | None = None) -> Path:
    """A live home stamped back to v9 with a hand-written state.json carrying the
    old status/halt vocabulary."""
    ch = Quest.create(control, title="", quest_id=cid, base_branch="main")
    (ch.state_dir / "state.json").write_text(json.dumps({
        "id": cid, "step": "s", "step_attempt": 0, "status": status,
        "halt_condition": halt_condition, "vars": {}, "loop_counts": {},
        "current_step": None, "retry_feedback": None,
    }, indent=2, sort_keys=True))
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 9
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return ch.quest_dir


def _state(quest_dir: Path) -> dict:
    return json.loads((quest_dir / "state" / "state.json").read_text())


def _meta(quest_dir: Path) -> dict:
    return json.loads((quest_dir / "quest.json").read_text())


def test_completed_becomes_merged(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v9(tmp_path, "mig1", status="completed")
    _migrate_v9_to_v10(qd)
    assert _state(qd)["status"] == "merged"
    assert _meta(qd)["layout_version"] == 10


def test_saved_becomes_paused(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v9(tmp_path, "mig2", status="saved")
    _migrate_v9_to_v10(qd)
    assert _state(qd)["status"] == "paused"


def test_paused_error_halt_becomes_error_status(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v9(tmp_path, "mig3", status="paused", halt_condition="error")
    _migrate_v9_to_v10(qd)
    st = _state(qd)
    assert st["status"] == "error"
    assert st["halt_condition"] is None


def test_residual_error_halt_is_cleared(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v9(tmp_path, "mig4", status="merge_blocked", halt_condition="error")
    _migrate_v9_to_v10(qd)
    st = _state(qd)
    assert st["status"] == "merge_blocked"
    assert st["halt_condition"] is None


def test_clean_paused_is_unchanged(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v9(tmp_path, "mig5", status="paused", halt_condition=None)
    _migrate_v9_to_v10(qd)
    st = _state(qd)
    assert st["status"] == "paused"
    assert st["halt_condition"] is None


def test_unmaterialized_seed_advances_version_only(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v9(tmp_path, "mig6", status="new")
    (qd / "state" / "state.json").unlink()   # un-materialized: no state.json
    _migrate_v9_to_v10(qd)
    assert _meta(qd)["layout_version"] == 10
    assert not (qd / "state" / "state.json").exists()


def test_load_migrates_v9_home_end_to_end(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v9(tmp_path, "mig7", status="completed")
    Quest.load(tmp_path, "mig7")             # triggers the migration chain on load
    assert _state(qd)["status"] == "merged"
    assert _meta(qd)["layout_version"] == LAYOUT_VERSION


def test_hop_is_idempotent_and_pins_layout_version(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v9(tmp_path, "mig8", status="completed")
    _migrate_v9_to_v10(qd)
    _migrate_v9_to_v10(qd)                   # second run must not corrupt or re-change
    assert _state(qd)["status"] == "merged"
    assert not list(qd.glob(".state-*")) and not list(qd.glob(".quest-*"))
    assert LAYOUT_VERSION == 19
