from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.engine import quest as quest_mod
from imp.engine.quest import LAYOUT_VERSION, Quest, _migrate_v15_to_v16
from tests.conftest import _seed_control

_V15_EVENTS = [
    {"id": "e1", "parent_id": None, "agent_id": "A", "timestamp": "T1",
     "type": "tool_call",
     "payload": {"id": "item_1", "name": "sub_agent",
                 "input": {"subagent_name": "explorer", "task_description": "x"}}},
    {"id": "e2", "parent_id": "e1", "agent_id": "A", "timestamp": "T2",
     "type": "tool_result",
     "payload": {"id": "item_1", "id_ns": "event", "content": "done", "error": False}},
]


def _seed_v15(control: Path, cid: str) -> Path:
    """A live home carrying an event-keyed codex fold in events.jsonl, stamped back
    to v15 — exactly what codex wrote before the id_ns fix."""
    ch = Quest.create(control, title="", quest_id=cid, base_branch="main")
    ch.events_path.write_text(
        "".join(json.dumps(e) + "\n" for e in _V15_EVENTS), encoding="utf-8")
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 15
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return ch.quest_dir


def _meta(quest_dir: Path) -> dict:
    return json.loads((quest_dir / "quest.json").read_text())


def _stamps(quest_dir: Path) -> list[str | None]:
    text = (quest_dir / "events.jsonl").read_text()
    return [json.loads(ln)["payload"].get("id_ns")
            for ln in text.splitlines() if ln.strip()]


def test_load_restamps_the_fold_as_block_and_stamps_the_version(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v15(tmp_path, "mv16a")

    Quest.load(tmp_path, "mv16a")          # triggers the migration chain on load

    assert _stamps(qd) == ["block", "block"]
    assert _meta(qd)["layout_version"] == LAYOUT_VERSION


def test_the_hop_is_registered_and_the_constant_moved() -> None:
    assert LAYOUT_VERSION == 22
    assert quest_mod._MIGRATIONS[(15, 16)] is _migrate_v15_to_v16


def test_a_failed_rewrite_leaves_the_home_at_fifteen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The version stamp lands AFTER the event rewrite, so a rewrite that dies takes
    # the whole hop with it and the home re-migrates on the next load.
    _seed_control(tmp_path)
    qd = _seed_v15(tmp_path, "mv16b")

    def _boom(path: Path, events: list[dict]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(quest_mod, "_rewrite_events_jsonl", _boom)
    with pytest.raises(OSError):
        _migrate_v15_to_v16(qd)

    assert _meta(qd)["layout_version"] == 15
    assert _stamps(qd) == [None, "event"]      # log untouched too


def test_second_run_is_a_no_op_and_leaves_no_temp_file(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v15(tmp_path, "mv16c")
    _migrate_v15_to_v16(qd)
    after_first = (qd / "events.jsonl").read_bytes()

    _migrate_v15_to_v16(qd)

    assert (qd / "events.jsonl").read_bytes() == after_first
    assert not list(qd.glob(".events-*"))      # atomic rewrite cleaned up after itself


def test_home_with_nothing_to_restamp_is_left_byte_identical(tmp_path: Path) -> None:
    # The overwhelmingly common case: no event-keyed fold anywhere. The hop must not
    # re-serialize the log at all — without the `if migrated != events` guard the
    # malformed line below would be replaced by its parsed {"_malformed": ...} stand-in,
    # changing the file bytes. That is the discriminating signal: removing the guard
    # must make this test fail.
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="", quest_id="mv16d", base_branch="main")
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 15
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    # Append a syntactically invalid JSON line; parse_events_jsonl turns it into
    # {"_malformed": true, "raw": "..."} which _rewrite_events_jsonl would persist
    # in its place if the guard were absent.
    ch.events_path.write_bytes(ch.events_path.read_bytes() + b"{not json\n")
    before = ch.events_path.read_bytes()

    _migrate_v15_to_v16(ch.quest_dir)

    assert ch.events_path.read_bytes() == before
    assert _meta(ch.quest_dir)["layout_version"] == 16
