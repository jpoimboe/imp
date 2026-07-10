from __future__ import annotations

import json
from pathlib import Path

from imp.engine.archive import read as archive_read
from imp.engine.change import live_quest_ids, live_stems


def _live_home(control: Path, cid: str) -> None:
    home = control / ".imp" / "changes" / cid
    home.mkdir(parents=True)
    (home / "change.json").write_text(json.dumps({"id": cid, "title": cid}))


def _archive_one(control: Path, cid: str) -> None:
    from imp.engine import archive

    events = control / f"{cid}.jsonl"
    events.write_bytes(
        f'{{"id":"{cid}-e0","parent_id":null,"agent_id":null,'
        '"timestamp":"2026-06-01T00:00:00+00:00","type":"change_created",'
        f'"payload":{{"id":"{cid}"}}}}\n'.encode()
    )
    cj = control / f"{cid}-change.json"
    cj.write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    sj = control / f"{cid}-state.json"
    sj.write_text(json.dumps({"id": cid, "status": "completed", "step": None}))
    archive.archive_change(
        control, cid, events_path=events,
        change_json_path=cj, state_json_path=sj)


def test_live_quest_ids_lists_only_homes_with_change_json(tmp_path: Path) -> None:
    _live_home(tmp_path, "wibo10")
    _live_home(tmp_path, "kobe47")
    # A stray dir without change.json and a hidden dir are both excluded.
    (tmp_path / ".imp" / "changes" / "junk").mkdir(parents=True)
    (tmp_path / ".imp" / "changes" / ".hidden").mkdir()
    assert live_quest_ids(tmp_path) == ["kobe47", "wibo10"]


def test_live_quest_ids_empty_when_no_changes_dir(tmp_path: Path) -> None:
    assert live_quest_ids(tmp_path) == []


def test_live_stems_are_the_four_letter_prefixes(tmp_path: Path) -> None:
    _live_home(tmp_path, "wibo10")
    _live_home(tmp_path, "kobe47")
    assert live_stems(tmp_path) == frozenset({"wibo", "kobe"})


def test_live_stems_exclude_the_archive(tmp_path: Path) -> None:
    # AC 5: a live home's stem is present; an archived id's stem is absent.
    _live_home(tmp_path, "wibo10")
    _archive_one(tmp_path, "zaka20")
    assert "zaka20" in archive_read.archived_ids(tmp_path)  # guard: it IS archived
    stems = live_stems(tmp_path)
    assert "wibo" in stems
    assert "zaka" not in stems


def test_list_quest_homes_delegates_to_live_quest_ids(tmp_path: Path) -> None:
    from imp.cli.main import _list_quest_homes

    _live_home(tmp_path, "wibo10")
    _live_home(tmp_path, "kobe47")
    assert _list_quest_homes(tmp_path) == ["kobe47", "wibo10"]


def test_mint_composition_avoids_live_stems(tmp_path: Path) -> None:
    from imp.engine.quest_id import new_quest_id

    for cid in ("kobe10", "mira20", "tavo30"):
        _live_home(tmp_path, cid)
    avoid = live_stems(tmp_path)
    for _ in range(20000):
        assert new_quest_id(existing_stems=avoid)[:4] not in avoid
