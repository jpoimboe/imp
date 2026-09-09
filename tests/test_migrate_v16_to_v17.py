from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.engine import quest as quest_mod
from imp.engine.quest import LAYOUT_VERSION, Quest, _migrate_v16_to_v17
from tests.conftest import _seed_control

_A = "step:build/coder"

_V16_EVENTS = [
    {"id": "m1", "parent_id": None, "agent_id": _A, "timestamp": "T1",
     "type": "agent_message",
     "payload": {"provider": "claude_code", "model": "claude-sonnet-4-6",
                 "usage": {"input_tokens": 10}, "stop_reason": "end_turn"}},
    {"id": "q1", "parent_id": None, "agent_id": _A, "timestamp": "T2",
     "type": "query_result",
     "payload": {"total_cost_usd": 3.00, "usage": {}, "model_usage": {}}},
    {"id": "q2", "parent_id": None, "agent_id": _A, "timestamp": "T3",
     "type": "query_result",
     "payload": {"total_cost_usd": 12.50, "usage": {}, "model_usage": {}}},
    {"id": "s1", "parent_id": None, "agent_id": None, "timestamp": "T4",
     "type": "step_started",
     "payload": {"phase": "build", "step": "coder", "provider": "claude_code",
                 "model": "claude-sonnet-4-6"}},
    {"id": "q3", "parent_id": None, "agent_id": _A, "timestamp": "T5",
     "type": "query_result",
     "payload": {"total_cost_usd": 6.40, "usage": {}, "model_usage": {}}},
]


def _seed_v16(control: Path, cid: str) -> Path:
    """A live home whose query_result rows predate the session_id field, stamped
    back to v16 — exactly what claude_code wrote before this quest."""
    ch = Quest.create(control, title="", quest_id=cid, base_branch="main")
    ch.events_path.write_text(
        "".join(json.dumps(e) + "\n" for e in _V16_EVENTS), encoding="utf-8")
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 16
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return ch.quest_dir


def _meta(quest_dir: Path) -> dict:
    return json.loads((quest_dir / "quest.json").read_text())


def _events(quest_dir: Path) -> list[dict]:
    text = (quest_dir / "events.jsonl").read_text()
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def _cost_by_session(quest_dir: Path) -> dict[str, float]:
    """Cumulative rows collapse to the last one per session — what Part 4 sums."""
    last: dict[str, float] = {}
    for e in _events(quest_dir):
        if e["type"] == "query_result":
            last[e["payload"]["session_id"]] = e["payload"]["total_cost_usd"]
    return last


def test_load_backfills_session_ids_and_stamps_the_version(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v16(tmp_path, "mv17a")

    Quest.load(tmp_path, "mv17a")          # triggers the migration chain on load

    ids = [e["payload"]["session_id"]
           for e in _events(qd) if e["type"] == "query_result"]
    assert ids[0] == ids[1]                # one cumulative series
    assert ids[1] != ids[2]                # the step_started closed it
    # 12.50 + 6.40 — the last row of each session, not the naive 21.90
    assert round(sum(_cost_by_session(qd).values()), 2) == 18.90
    assert _meta(qd)["layout_version"] == LAYOUT_VERSION


def test_the_migration_adds_session_ids_only(tmp_path: Path) -> None:
    # Spec §4: auth mode is deliberately NOT backfilled — it is unrecoverable, and
    # an absent value already renders notional.
    _seed_control(tmp_path)
    qd = _seed_v16(tmp_path, "mv17b")

    _migrate_v16_to_v17(qd)

    events = _events(qd)
    assert events[0] == _V16_EVENTS[0]     # agent_message untouched, no auth_mode
    assert events[3] == _V16_EVENTS[3]     # step_started untouched
    assert set(events[1]["payload"]) == {
        "session_id", "total_cost_usd", "usage", "model_usage"}


def test_the_hop_is_registered_and_the_constant_moved() -> None:
    assert LAYOUT_VERSION == 19
    assert quest_mod._MIGRATIONS[(16, 17)] is _migrate_v16_to_v17


def test_a_failed_rewrite_leaves_the_home_at_sixteen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The version stamp lands AFTER the event rewrite, so a rewrite that dies takes
    # the whole hop with it and the home re-migrates on the next load.
    _seed_control(tmp_path)
    qd = _seed_v16(tmp_path, "mv17c")

    def _boom(path: Path, events: list[dict]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(quest_mod, "_rewrite_events_jsonl", _boom)
    with pytest.raises(OSError):
        _migrate_v16_to_v17(qd)

    assert _meta(qd)["layout_version"] == 16
    assert all("session_id" not in e["payload"] for e in _events(qd))


def test_second_run_is_a_no_op_and_leaves_no_temp_file(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v16(tmp_path, "mv17d")
    _migrate_v16_to_v17(qd)
    after_first = (qd / "events.jsonl").read_bytes()

    _migrate_v16_to_v17(qd)

    assert (qd / "events.jsonl").read_bytes() == after_first
    assert not list(qd.glob(".events-*"))      # atomic rewrite cleaned up after itself


def test_home_with_no_query_result_is_left_byte_identical(tmp_path: Path) -> None:
    # The overwhelmingly common case: no claude_code rows at all. The hop must not
    # re-serialize the log — without the `if migrated != events` guard the malformed
    # line below would be replaced by its parsed {"_malformed": ...} stand-in,
    # changing the file bytes. Removing the guard must make this test fail.
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="", quest_id="mv17e", base_branch="main")
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 16
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    ch.events_path.write_bytes(ch.events_path.read_bytes() + b"{not json\n")
    before = ch.events_path.read_bytes()

    _migrate_v16_to_v17(ch.quest_dir)

    assert ch.events_path.read_bytes() == before
    assert _meta(ch.quest_dir)["layout_version"] == 17
