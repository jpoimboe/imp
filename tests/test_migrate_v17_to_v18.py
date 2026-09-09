from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.engine import quest as quest_mod
from imp.engine.quest import LAYOUT_VERSION, Quest, _migrate_v17_to_v18
from tests.conftest import _seed_control

_A = "step:planning/plan-reviewer"

_V17_EVENTS = [
    {"id": "s1", "parent_id": None, "agent_id": None, "timestamp": "T1",
     "type": "step_started",
     "payload": {"step": "plan-reviewer", "kind": "agent", "phase": "planning",
                 "show_label": True, "show_output": False,
                 "label": "reviewing plan"}},
    {"id": "m1", "parent_id": None, "agent_id": _A, "timestamp": "T2",
     "type": "agent_message",
     "payload": {"provider": "anthropic", "model": "m",
                 "usage": {"input_tokens": 10}, "stop_reason": "end_turn"}},
    {"id": "s2", "parent_id": None, "agent_id": None, "timestamp": "T3",
     "type": "step_started",
     "payload": {"step": "plan-reviewer", "kind": "agent", "phase": "planning",
                 "show_label": True, "show_output": False,
                 "label": "reviewing plan"}},
]


def _seed_v17(control: Path, cid: str) -> Path:
    """A live home whose step_started rows predate the `round` field, stamped back
    to v17 — exactly what the engine wrote before this quest."""
    ch = Quest.create(control, title="", quest_id=cid, base_branch="main")
    ch.events_path.write_text(
        "".join(json.dumps(e) + "\n" for e in _V17_EVENTS), encoding="utf-8")
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 17
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return ch.quest_dir


def _meta(quest_dir: Path) -> dict:
    return json.loads((quest_dir / "quest.json").read_text())


def _events(quest_dir: Path) -> list[dict]:
    text = (quest_dir / "events.jsonl").read_text()
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def test_load_stamps_round_one_on_every_step_started(tmp_path: Path) -> None:
    # Including the repeat entry of a looping step: rounds are deliberately NOT
    # reconstructed, and 1 is the value that renders no segment.
    _seed_control(tmp_path)
    qd = _seed_v17(tmp_path, "mv18a")

    Quest.load(tmp_path, "mv18a")          # triggers the migration chain on load

    assert [e["payload"]["round"]
            for e in _events(qd) if e["type"] == "step_started"] == [1, 1]
    assert _meta(qd)["layout_version"] == LAYOUT_VERSION


def test_the_migration_adds_round_only(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v17(tmp_path, "mv18b")

    _migrate_v17_to_v18(qd)

    events = _events(qd)
    assert events[1] == _V17_EVENTS[1]     # agent_message byte-identical
    assert events[0]["payload"] == {**_V17_EVENTS[0]["payload"], "round": 1}
    assert {k: v for k, v in events[0].items() if k != "payload"} == {
        k: v for k, v in _V17_EVENTS[0].items() if k != "payload"}


def test_the_hop_is_registered_and_the_constant_moved() -> None:
    assert LAYOUT_VERSION == 20
    assert quest_mod._MIGRATIONS[(17, 18)] is _migrate_v17_to_v18


def test_a_failed_rewrite_leaves_the_home_at_seventeen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The version stamp lands AFTER the event rewrite, so a rewrite that dies takes
    # the whole hop with it and the home re-migrates on the next load.
    _seed_control(tmp_path)
    qd = _seed_v17(tmp_path, "mv18c")

    def _boom(path: Path, events: list[dict]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(quest_mod, "_rewrite_events_jsonl", _boom)
    with pytest.raises(OSError):
        _migrate_v17_to_v18(qd)

    assert _meta(qd)["layout_version"] == 17
    assert all("round" not in e["payload"] for e in _events(qd))


def test_second_run_is_a_no_op_and_leaves_no_temp_file(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v17(tmp_path, "mv18d")
    _migrate_v17_to_v18(qd)
    after_first = (qd / "events.jsonl").read_bytes()

    _migrate_v17_to_v18(qd)

    assert (qd / "events.jsonl").read_bytes() == after_first
    assert not list(qd.glob(".events-*"))      # atomic rewrite cleaned up after itself


def test_home_with_no_step_started_is_left_byte_identical(tmp_path: Path) -> None:
    # The hop must not re-serialize a log it has nothing to add to — without the
    # `if migrated != events` guard the malformed line below would be replaced by
    # its parsed {"_malformed": ...} stand-in, changing the file bytes.
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="", quest_id="mv18e", base_branch="main")
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 17
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    ch.events_path.write_bytes(
        json.dumps(_V17_EVENTS[1]).encode("utf-8") + b"\n{not json\n")
    before = ch.events_path.read_bytes()

    _migrate_v17_to_v18(ch.quest_dir)

    assert ch.events_path.read_bytes() == before
    assert _meta(ch.quest_dir)["layout_version"] == 18
