from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.engine import quest as quest_mod
from imp.engine.quest import LAYOUT_VERSION, Quest, _migrate_v18_to_v19
from imp.engine.state import State
from tests.conftest import _seed_control

_V18_HISTORY = {
    "plan-writer": [
        {"lap": 1, "outcome": "wrote plan-1", "head": "aaaa1111bbbb2222"},
        {"lap": 2, "outcome": "revised plan-1", "head": "cccc3333dddd4444"},
    ],
    "plan-reviewer": [
        {"lap": 1, "outcome": "sent back", "head": "eeee5555ffff6666"},
    ],
}


def _seed_v18(control: Path, cid: str, *, compact: bool = False,
              **state_extra: object) -> Path:
    """A live home stamped back to v18 with a hand-written state.json whose
    outcome_history entries still carry the pre-removal `head` sha."""
    ch = Quest.create(control, title="", quest_id=cid, base_branch="main")
    payload = {
        "id": cid, "step": "plan-writer", "step_attempt": 0, "status": "paused",
        "vars": {"plan_index": 2}, "loop_counts": {"planning": 1},
        "current_step": None, "retry_feedback": None,
        "outcome_history": json.loads(json.dumps(_V18_HISTORY)),
        **state_extra,
    }
    (ch.state_dir / "state.json").write_text(
        json.dumps(payload) if compact
        else json.dumps(payload, indent=2, sort_keys=True))
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 18
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return ch.quest_dir


def _state(quest_dir: Path) -> dict:
    return json.loads((quest_dir / "state" / "state.json").read_text())


def _meta(quest_dir: Path) -> dict:
    return json.loads((quest_dir / "quest.json").read_text())


def test_the_hop_is_registered_and_the_constant_moved() -> None:
    assert LAYOUT_VERSION == 23
    assert quest_mod._MIGRATIONS[(18, 19)] is _migrate_v18_to_v19


def test_every_entry_loses_head_and_keeps_lap_and_outcome(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v18(tmp_path, "mv19a")

    _migrate_v18_to_v19(qd)

    hist = _state(qd)["outcome_history"]
    assert hist == {
        "plan-writer": [
            {"lap": 1, "outcome": "wrote plan-1"},
            {"lap": 2, "outcome": "revised plan-1"},
        ],
        "plan-reviewer": [{"lap": 1, "outcome": "sent back"}],
    }
    assert _meta(qd)["layout_version"] == 19


def test_the_rest_of_state_json_round_trips_unchanged(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v18(tmp_path, "mv19b", memory={"all.md": "note"},
                   merge_commit="abc123")

    _migrate_v18_to_v19(qd)

    st = _state(qd)
    assert {k: v for k, v in st.items() if k != "outcome_history"} == {
        "id": "mv19b", "step": "plan-writer", "step_attempt": 0,
        "status": "paused", "vars": {"plan_index": 2},
        "loop_counts": {"planning": 1}, "current_step": None,
        "retry_feedback": None, "memory": {"all.md": "note"},
        "merge_commit": "abc123",
    }


def test_migrated_state_loads_with_the_bounded_history(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v18(tmp_path, "mv19c")

    _migrate_v18_to_v19(qd)

    state = State.load(qd / "state")
    assert state.outcome_history["plan-writer"][-1] == {
        "lap": 2, "outcome": "revised plan-1"}


def test_load_migrates_a_v18_home_end_to_end(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v18(tmp_path, "mv19d")

    Quest.load(tmp_path, "mv19d")          # triggers the migration chain on load

    assert all("head" not in e
               for entries in _state(qd)["outcome_history"].values()
               for e in entries)
    assert _meta(qd)["layout_version"] == LAYOUT_VERSION


def test_second_run_is_a_no_op_and_leaves_no_temp_file(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v18(tmp_path, "mv19e")
    _migrate_v18_to_v19(qd)
    after_first = (qd / "state" / "state.json").read_bytes()

    _migrate_v18_to_v19(qd)

    assert (qd / "state" / "state.json").read_bytes() == after_first
    assert not list((qd / "state").glob(".state-*"))
    assert not list(qd.glob(".quest-*"))


def test_a_home_with_no_head_keys_is_left_byte_identical(tmp_path: Path) -> None:
    # Seeded compact, so an unconditional rewrite would re-indent the file and
    # change its bytes: the guard is what keeps a nothing-to-do home untouched.
    _seed_control(tmp_path)
    qd = _seed_v18(tmp_path, "mv19f", compact=True,
                   outcome_history={"w": [{"lap": 1, "outcome": "done"}]})
    before = (qd / "state" / "state.json").read_bytes()

    _migrate_v18_to_v19(qd)

    assert (qd / "state" / "state.json").read_bytes() == before
    assert _meta(qd)["layout_version"] == 19


def test_a_home_with_no_state_json_advances_only_the_stamp(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v18(tmp_path, "mv19g")
    (qd / "state" / "state.json").unlink()

    _migrate_v18_to_v19(qd)

    assert not (qd / "state" / "state.json").exists()
    assert _meta(qd)["layout_version"] == 19


def test_a_failed_rewrite_leaves_the_home_at_eighteen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The version stamp lands AFTER the state rewrite, so a rewrite that dies
    # takes the whole hop with it and the home re-migrates on the next load.
    # The surviving `head` below is the atomicity pin, not a claim that the key
    # is still supported — an un-stamped home must still hold its v18 bytes.
    _seed_control(tmp_path)
    qd = _seed_v18(tmp_path, "mv19h")

    def _boom(state_path: Path, data: dict) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(quest_mod, "_rewrite_state_json", _boom)
    with pytest.raises(OSError):
        _migrate_v18_to_v19(qd)

    assert _meta(qd)["layout_version"] == 18
    assert _state(qd)["outcome_history"]["plan-writer"][0]["head"] == (
        "aaaa1111bbbb2222")
