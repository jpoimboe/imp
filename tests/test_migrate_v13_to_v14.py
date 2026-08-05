from __future__ import annotations

import json
from pathlib import Path

from imp.engine.quest import LAYOUT_VERSION, Quest, _migrate_v13_to_v14
from imp.engine.state import State
from tests.conftest import _seed_control


def _seed_v13(control: Path, cid: str, **state_extra: object) -> Path:
    """A live home stamped back to v13 with a hand-written state.json carrying
    the pre-removal project_facts key."""
    ch = Quest.create(control, title="", quest_id=cid, base_branch="main")
    (ch.state_dir / "state.json").write_text(json.dumps({
        "id": cid, "step": "code", "step_attempt": 0, "status": "paused",
        "vars": {}, "loop_counts": {}, "current_step": None,
        "retry_feedback": None, **state_extra,
    }, indent=2, sort_keys=True))
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 13
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return ch.quest_dir


def _state(quest_dir: Path) -> dict:
    return json.loads((quest_dir / "state" / "state.json").read_text())


def _meta(quest_dir: Path) -> dict:
    return json.loads((quest_dir / "quest.json").read_text())


def test_legacy_key_is_dropped_and_frozen_memory_survives(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v13(tmp_path, "mf1",
                   project_facts={"stack": "python"}, memory={"all.md": "note"})
    _migrate_v13_to_v14(qd)
    st = _state(qd)
    assert "project_facts" not in st
    assert st["memory"] == {"all.md": "note"}      # untouched
    assert _meta(qd)["layout_version"] == 14


def test_home_frozen_under_the_old_gate_gets_an_empty_memory(tmp_path: Path) -> None:
    # Frozen (project_facts present and non-null) but no memory key: the new gate
    # reads state.memory, so it must come out {} and NOT re-freeze.
    _seed_control(tmp_path)
    qd = _seed_v13(tmp_path, "mf2", project_facts={})
    _migrate_v13_to_v14(qd)
    assert _state(qd)["memory"] == {}


def test_null_memory_on_a_frozen_home_becomes_empty(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v13(tmp_path, "mf3", project_facts={"a": "1"}, memory=None)
    _migrate_v13_to_v14(qd)
    assert _state(qd)["memory"] == {}


def test_never_frozen_home_stays_unfrozen(tmp_path: Path) -> None:
    # project_facts null == the first run never happened: memory must stay None
    # so the next run freezes normally.
    _seed_control(tmp_path)
    qd = _seed_v13(tmp_path, "mf4", project_facts=None, memory=None)
    _migrate_v13_to_v14(qd)
    assert _state(qd)["memory"] is None


def test_migrated_state_loads_without_the_field(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v13(tmp_path, "mf5", project_facts={"a": "1"}, memory={})
    _migrate_v13_to_v14(qd)
    state = State.load(qd / "state")
    assert not hasattr(state, "project_facts")
    assert state.memory == {}


def test_load_migrates_v13_home_end_to_end(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v13(tmp_path, "mf6", project_facts={"a": "1"}, memory={"all.md": "n"})
    Quest.load(tmp_path, "mf6")               # triggers the migration chain on load
    assert "project_facts" not in _state(qd)
    assert _meta(qd)["layout_version"] == LAYOUT_VERSION


def test_hop_is_idempotent_and_pins_layout_version(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v13(tmp_path, "mf7", project_facts={"a": "1"}, memory={"all.md": "n"})
    _migrate_v13_to_v14(qd)
    _migrate_v13_to_v14(qd)                   # second run must not corrupt or re-change
    st = _state(qd)
    assert "project_facts" not in st
    assert st["memory"] == {"all.md": "n"}
    assert not list((qd / "state").glob(".state-*")) and not list(qd.glob(".quest-*"))
    assert LAYOUT_VERSION == 15
